"""Course website scraper.

Many Berkeley courses run off their own site rather than Canvas — data89.org,
ds100.org, cs61a.org and so on. Most are built from the same "Just the Class"
Jekyll template, which renders a calendar as a table of dated rows:

    | Mon, Sep 07 | **Homework 2** Distributions and Models |
    | Mon, Sep 28 | **Exam** Midterm 1   **Homework 5** Variance and SD |

That structure is regular enough to parse DETERMINISTICALLY. No LLM is used
here, which matters: the date comes from the row it was found in and the title
from the bolded label, so nothing can be invented. If the page doesn't match
this shape, we return nothing and say so rather than guessing.

Run:
    uv run python -m expense_agent.course_site
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import requests

import os as _os
_AGENT_ROOT = Path(_os.environ.get('STATE_DIR')) \
    if _os.environ.get('STATE_DIR') \
    else Path(__file__).resolve().parent.parent
SITE_CACHE = _AGENT_ROOT / "course_sites.json"

# Bolded labels that represent work with a deadline. Lectures and holidays are
# schedule entries, not tasks — including them would bury the real work.
GRADED_LABELS = {
    "homework": "problem set",
    "hw": "problem set",
    "hw": "problem set",
    "hw": "problem set",
    "problem set": "problem set",
    "exam": "exam",
    "midterm": "exam",
    "final": "exam",
    "quiz": "quiz",
    "quiz retake": "quiz",
    "project": "project",
    "lab": "lab",
    "discussion": "discussion",
    "survey": "other",
}

SKIP_LABELS = {
    "lecture", "holiday", "rrr week", "finals week", "no discussion",
    "no lecture", "reading", "optional",
}

# "Mon, Sep 07" / "Thu, Aug 27" / "Tue, Dec 15"
RESOURCE_CELL = ("(tbd)", "slides", "worksheet", "demo", "lecture code",
                 "annotated", "practice problems", "solutions")

RELEASE_MARKERS = ("released", "assigned", "posted", "handed out")

_DATE_RE = re.compile(
    r"\b(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*,?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b",
    re.I,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# **Homework 2**Distributions  ->  label "Homework 2", trailing text the topic
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*([^*|]*)")


def _strip_html(html: str) -> str:
    """HTML -> text, keeping table pipes so rows stay distinguishable."""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    # bold tags become markdown so one regex handles both HTML and markdown
    text = re.sub(r"</?(strong|b)>", "**", text, flags=re.I)
    text = re.sub(r"</t[dh]>", " | ", text, flags=re.I)
    text = re.sub(r"</tr>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _classify(label: str) -> str | None:
    """Map a bolded label to a task type, or None if it isn't work."""
    low = label.lower().strip()
    if any(s in low for s in SKIP_LABELS):
        return None
    for key, kind in GRADED_LABELS.items():
        if low.startswith(key):
            return kind
    return None


def _guess_year(month: int, today: dt.date) -> int:
    """A course site rarely states the year. Assume the academic year in play.

    Aug-Dec belongs to the current calendar year in the fall; Jan-Jul that
    follows belongs to the next one.
    """
    if today.month >= 8:
        return today.year if month >= 8 else today.year + 1
    return today.year if month <= 7 else today.year - 1


def _assignment_key(title: str) -> str:
    """Collapse the many ways one assignment is written on a page.

    A single row often names the same work twice - once as the bolded label
    ("HW 1") and again in the resource link ("Homework 1 (TBD)"). Reducing
    both to "hw1" makes them collide so only one task survives.
    """
    t = title.lower()
    t = re.sub(r"\(tbd\)|\(tentative\)|due|released", " ", t)
    t = t.replace("homework", "hw").replace("problem set", "ps")
    t = t.replace("discussion", "disc").replace("quiz retake", "qretake")
    m = re.search(r"\b(hw|ps|disc|quiz|qretake|exam|midterm|lab)\s*(\d+)", t)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return re.sub(r"[^a-z0-9]+", "", t)[:28]


def parse_schedule(text: str, course: str) -> list[dict]:
    """Pull dated work out of a course page. Deterministic — no model calls.

    Real pages put each table cell on its own line, so a date and the items
    under it are rarely on the SAME line. We carry the most recent date
    forward and attach items to it until the next date appears — which is
    exactly how the rendered table reads top to bottom.
    """
    today = dt.date.today()
    found: list[dict] = []
    seen: set = set()
    current: dt.date | None = None

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        m = _DATE_RE.search(stripped)
        if m:
            month = _MONTHS.get(m.group(1).lower()[:4].rstrip("."))
            if not month:
                month = _MONTHS.get(m.group(1).lower()[:3])
            if month:
                day = int(m.group(2))
                try:
                    current = dt.date(_guess_year(month, today), month, day)
                except ValueError:
                    current = None
            rest = stripped[m.end():]
        else:
            rest = stripped

        if current is None:
            continue

        # Bolded labels, when the page kept its markup...
        pairs = _BOLD_RE.findall(rest)
        # ...and bare lines like "Homework 1" when the markup was stripped.
        if not pairs and rest:
            pairs = [(rest, "")]

        for bold, trailing in pairs:
            kind = _classify(bold)
            if not kind:
                continue
            if any(mk in (bold + " " + trailing).lower() for mk in RELEASE_MARKERS):
                continue
            # skip resource cells that echo the assignment name
            if bold.strip().lower() in ("slides","worksheet","demo","lecture code","solutions"):
                continue
            title = re.sub(r"\s+", " ", bold.replace("**", " ")).strip(" |·-–—")
            topic = re.sub(r"\s+", " ", trailing.replace("**", " ")).strip(" |·-–—")
            if topic.upper().rstrip(":()") in ("DUE", ""): topic = ""
            topic = re.sub(r"\s*\(TBD\)\s*", " ", topic).strip(" |·-–—")
            if topic and len(topic) < 60 and not topic.lower().startswith("http"):
                title = f"{title}: {topic}"
            if len(title) < 3 or len(title) > 90:
                continue
            ak = _assignment_key(title)
            key = (ak,) if kind == "exam" else (ak, current)
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "title": title[:90],
                "type": kind,
                "due_date": current.isoformat(),
                "course": course,
                "evidence": stripped[:160],
            })

    # A multi-day exam window matters on its final day - that is the last
    # chance to sit it. Keyed on title, so keep the latest date we saw.
    window_end = {}
    for it in found:
        if it["type"] != "exam":
            continue
        k = it["title"].lower()
        if k not in window_end or it["due_date"] > window_end[k]:
            window_end[k] = it["due_date"]
    for it in found:
        if it["type"] == "exam":
            it["due_date"] = window_end[it["title"].lower()]

    return found


def scrape_course_site(course: str, url: str) -> dict:
    """Fetch a course site and pull its dated work.

    Returns a result dict that always says what happened — if the page doesn't
    parse, that's reported rather than silently returning an empty list.
    """
    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0 Safari/537.36")
        })
        resp.raise_for_status()
    except Exception as e:
        return {"course": course, "url": url, "ok": False,
                "reason": f"couldn't fetch: {str(e)[:70]}", "items": []}

    text = _strip_html(resp.text)
    items = parse_schedule(text, course)

    if not items:
        return {"course": course, "url": url, "ok": False,
                "reason": ("no dated schedule found on this page — the site may "
                           "load its calendar with JavaScript, or use a layout "
                           "this parser doesn't recognise"),
                "items": []}

    return {"course": course, "url": url, "ok": True,
            "reason": f"parsed {len(items)} dated items", "items": items}


def scrape_all() -> dict:
    """Scrape every course site the student has saved."""
    try:
        from .manual_sources import load_course_urls
        urls = load_course_urls()
    except Exception:
        urls = {}

    results = {}
    for course, url in urls.items():
        results[course] = scrape_course_site(course, url)

    SITE_CACHE.write_text(json.dumps({
        "scraped_at": dt.datetime.now().astimezone().isoformat(),
        "results": results,
    }, indent=2))
    return results


def site_tasks() -> list:
    """Course-site work as Task objects, for the scheduler."""
    from .models import Task

    if not SITE_CACHE.exists():
        return []
    try:
        data = json.loads(SITE_CACHE.read_text())
    except Exception:
        return []

    now = dt.datetime.now(dt.timezone.utc)
    tasks = []
    for course, res in (data.get("results") or {}).items():
        if not res.get("ok"):
            continue
        for it in res.get("items", []):
            try:
                d = dt.date.fromisoformat(it["due_date"])
                due = dt.datetime(d.year, d.month, d.day, 23, 59,
                                  tzinfo=dt.timezone.utc)
            except Exception:
                continue
            if due < now:
                continue
            tasks.append(Task(
                source="course_site",
                work_type="coursework",
                source_ref=f"site:{course[:20]}:{it['title'][:40]}",
                title=it["title"],
                course=course,
                description=it.get("evidence"),
                due_at=due,
            ))
    return tasks


if __name__ == "__main__":
    print("\nScraping saved course sites...\n")
    results = scrape_all()
    if not results:
        print("  No course sites saved yet. Add one from the web page.\n")
    for course, res in results.items():
        mark = "OK " if res["ok"] else "-- "
        print(f"  {mark} {course[:40]:<40} {res['reason']}")
        for it in res["items"][:10]:
            print(f"        {it['due_date']}  [{it['type']:<11}] {it['title'][:52]}")
        if len(res["items"]) > 10:
            print(f"        ... and {len(res['items']) - 10} more")
    print()
