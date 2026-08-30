"""Syllabus reader: difficulty estimation + assignment extraction.

Canvas exposes a course syllabus two ways:
  1. `syllabus_body` — HTML on the course's Syllabus page. Reliable, easy.
  2. Uploaded files (PDF/DOCX) in Files. Messy: some are scanned images that
     can't be parsed at all. We try, but don't depend on it.

For each course we:
  - fetch whatever syllabus text exists
  - ask Gemini for a difficulty rating (1-5) and workload estimate
  - ask Gemini to extract any assignments/deadlines mentioned in the syllabus
    that aren't already in Canvas's assignments list (readings, participation,
    weekly work — these often live ONLY in the syllabus)

Difficulty feeds the scheduler's time budgeting. Extracted assignments get
surfaced so you can decide whether to track them.

Run:
    uv run python -m expense_agent.syllabus
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

from .canvas_poller import _get, fetch_active_courses, CANVAS_BASE_URL, _headers

import os as _os
_AGENT_ROOT = Path(_os.environ.get('STATE_DIR')) \
    if _os.environ.get('STATE_DIR') \
    else Path(__file__).resolve().parent.parent
SYLLABUS_CACHE = _AGENT_ROOT / "syllabus_analysis.json"

# Gemini via the same key the agent already uses.
GEMINI_MODEL = os.environ.get("SYLLABUS_MODEL", "gemini-flash-latest")


# ---------------------------------------------------------------------------
# Fetching syllabus text
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Crude HTML -> text. Good enough for LLM input."""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_syllabus_body(course_id: int) -> str:
    """Get the course's Syllabus page text (the reliable path)."""
    try:
        data = _get(f"/courses/{course_id}", params={"include[]": "syllabus_body"})
        body = (data or {}).get("syllabus_body") or ""
        return _strip_html(body)
    except Exception:
        return ""


def find_syllabus_files(course_id: int) -> list[dict]:
    """Look for files that look like a syllabus (best-effort; often blocked)."""
    try:
        files = _get(f"/courses/{course_id}/files", params={"per_page": 100})
    except Exception:
        return []  # many courses restrict file listing to teachers
    hits = []
    for f in files or []:
        name = (f.get("display_name") or "").lower()
        if "syllabus" in name:
            hits.append(f)
    return hits


def fetch_file_text(file_obj: dict) -> str:
    """Try to extract text from a syllabus file. PDFs often fail; that's fine."""
    url = file_obj.get("url")
    name = (file_obj.get("display_name") or "").lower()
    if not url:
        return ""
    try:
        resp = requests.get(url, headers=_headers(), timeout=60)
        resp.raise_for_status()
    except Exception:
        return ""

    if name.endswith(".pdf"):
        try:
            import io
            from pypdf import PdfReader  # optional dependency
            reader = PdfReader(io.BytesIO(resp.content))
            return "\n".join((p.extract_text() or "") for p in reader.pages)[:20000]
        except Exception:
            return ""  # scanned image or pypdf not installed
    if name.endswith((".txt", ".md")):
        return resp.text[:20000]
    return ""


def gather_syllabus_text(course_id: int, course_name: str = "") -> tuple[str, str]:
    """Return (text, source_label).

    Checks, in order:
      1. a syllabus file the student dropped in syllabi/ — most reliable, since
         instructors usually hand out a PDF and leave Canvas's syllabus page blank
      2. the Canvas syllabus page
      3. syllabus files in Canvas Files (often blocked for students)
    """
    if course_name:
        try:
            from .manual_sources import (
                list_syllabus_files, match_course, extract_text, SYLLABI_DIR
            )
            hit = match_course(course_name, list_syllabus_files())
            if hit:
                text = extract_text(SYLLABI_DIR / hit["file"])
                if len(text) > 200:
                    return text[:20000], f"your file: {hit['file']}"
        except Exception:
            pass

    body = fetch_syllabus_body(course_id)
    if len(body) > 200:
        return body[:20000], "syllabus page"

    for f in find_syllabus_files(course_id):
        text = fetch_file_text(f)
        if len(text) > 200:
            return text[:20000], f"file: {f.get('display_name')}"

    return body, "none found" if not body else "syllabus page (short)"


# ---------------------------------------------------------------------------
# Gemini analysis
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """\
You are analyzing a university course syllabus.

Return ONLY a JSON object, no prose, no markdown fences:

{
  "difficulty": <integer 1-5>,
  "difficulty_reason": "<one short sentence>",
  "difficulty_evidence": "<VERBATIM quote from the syllabus, 5-25 words>",
  "weekly_hours_estimate": <number>,
  "assignments": [
    {
      "title": "<name>",
      "due_hint": "<date or 'weekly' or 'unknown'>",
      "type": "<reading|paper|exam|problem set|participation|project|other>",
      "evidence": "<VERBATIM quote from the syllabus that mentions this, 5-25 words>"
    }
  ]
}

CRITICAL GROUNDING RULES:
- Every `evidence` field MUST be copied EXACTLY, word for word, from the
  syllabus text below. Do not paraphrase, summarize, or reconstruct it.
- If you cannot find a verbatim quote supporting an assignment, DO NOT
  include that assignment at all.
- Never infer assignments from what a course "usually" has. Only list what
  this specific text states.
- If the syllabus has no usable content, return difficulty 3,
  weekly_hours_estimate 0, an empty assignments list, and empty evidence.

Other rules:
- difficulty: 1 = very light, 5 = very demanding, judged only from what the
  text actually says about workload, assessments, and reading load.
- weekly_hours_estimate: realistic out-of-class hours per week. Use 0 if the
  text gives you nothing to base it on.

SYLLABUS TEXT:
"""


def analyze_with_gemini(text: str) -> dict:
    """Send syllabus text to Gemini, parse the JSON response."""
    if len(text.strip()) < 200:
        return {
            "difficulty": 3,
            "difficulty_reason": "No usable syllabus text found.",
            "weekly_hours_estimate": 0,
            "assignments": [],
        }

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return {
            "difficulty": 3,
            "difficulty_reason": "No GOOGLE_API_KEY set.",
            "weekly_hours_estimate": 0,
            "assignments": [],
        }

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": ANALYSIS_PROMPT + text}]}],
        "generationConfig": {"temperature": 0.2},
    }
    try:
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        return json.loads(raw)
    except Exception as e:
        return {
            "difficulty": 3,
            "difficulty_reason": f"Analysis failed: {str(e)[:80]}",
            "weekly_hours_estimate": 0,
            "assignments": [],
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyze_all_courses(only_current: bool = True) -> dict:
    """Analyze syllabi for active courses. Returns {course_name: analysis}."""
    results = {}
    courses = fetch_active_courses()

    for c in courses:
        name = c.get("name") or ""
        cid = c.get("id")
        # Skip obviously old terms to save API calls
        if only_current and not re.search(r"(Fall 2026|Spring 2027|Summer 2026)", name):
            continue

        print(f"  Reading syllabus: {name[:50]}...")
        text, source = gather_syllabus_text(cid, name)
        analysis = analyze_with_gemini(text)
        analysis = verify_analysis(analysis, text)
        analysis["source"] = source
        analysis["course_id"] = cid
        results[name] = analysis

    SYLLABUS_CACHE.write_text(json.dumps(results, indent=2))
    return results


def difficulty_multipliers() -> dict:
    """Convert saved difficulty ratings into scheduler time multipliers.

    difficulty 1 -> 0.8x time, 3 -> 1.0x, 5 -> 1.4x
    """
    if not SYLLABUS_CACHE.exists():
        return {}
    try:
        data = json.loads(SYLLABUS_CACHE.read_text())
    except Exception:
        return {}
    out = {}
    for course, a in data.items():
        d = a.get("difficulty", 3)
        out[course] = round(0.8 + (d - 1) * 0.15, 2)
    return out


def print_report(results: dict) -> None:
    print("\n" + "=" * 78)
    print("  SYLLABUS ANALYSIS")
    print("=" * 78)
    for course, a in results.items():
        stars = "*" * a.get("difficulty", 3)
        print(f"\n  {course[:60]}")
        print(f"    Difficulty:  {stars:<5} ({a.get('difficulty')}/5)  "
              f"~{a.get('weekly_hours_estimate')}h/week")
        print(f"    Why:         {a.get('difficulty_reason', '')[:70]}")
        print(f"    Source:      {a.get('source')}")
        assignments = a.get("assignments", [])
        g = a.get("grounding", {})
        if assignments:
            print(f"    Found in syllabus ({len(assignments)}, all verified):")
            for x in assignments[:8]:
                print(f"      - {x.get('title','')[:45]:<45} "
                      f"[{x.get('type','')}] due: {x.get('due_hint','')}")
        if g.get("assignments_dropped"):
            print(f"    DISCARDED as unverifiable ({len(g['assignments_dropped'])}): "
                  f"{', '.join(g['assignments_dropped'][:4])}")
        if not g.get("difficulty_grounded", True):
            print("    Difficulty rating was not backed by the text -> defaulted to 3")
    print("\n" + "=" * 78)
    print(f"  Saved to {SYLLABUS_CACHE.name}")
    print("  Difficulty multipliers now available to the scheduler.")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    print("\nAnalyzing syllabi for current courses...\n")
    res = analyze_all_courses()
    print_report(res)


# ---------------------------------------------------------------------------
# Grounding verification
# ---------------------------------------------------------------------------
# Asking the model to cite sources is a request, not a guarantee. This is the
# enforcement: every claimed quote must actually appear in the syllabus text.
# Anything we can't verify is dropped rather than shown to the user.

def _normalize(s: str) -> str:
    """Loose normalization so trivial whitespace/punctuation diffs don't fail."""
    s = (s or "").lower()
    s = re.sub(r"[\u2018\u2019\u201c\u201d]", "'", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _quote_is_grounded(quote: str, source: str, min_words: int = 4) -> bool:
    """True if the quote genuinely appears in the source text.

    Exact substring match after normalization. Falls back to requiring a long
    contiguous run of the quote's words to appear, which tolerates the model
    trimming a word at either end but still rejects invention.
    """
    q = _normalize(quote)
    s = _normalize(source)
    if not q or len(q.split()) < min_words:
        return False
    if q in s:
        return True
    # allow a shortened window of the quote to match (model trimmed an edge)
    words = q.split()
    for size in range(len(words), min_words - 1, -1):
        for start in range(0, len(words) - size + 1):
            if " ".join(words[start:start + size]) in s:
                return True
    return False


def verify_analysis(analysis: dict, source_text: str) -> dict:
    """Drop any model claim that isn't backed by a verbatim quote.

    Adds a `grounding` report so the briefing can be honest about what was
    verified vs. discarded.
    """
    kept, dropped = [], []
    for a in analysis.get("assignments", []) or []:
        if _quote_is_grounded(a.get("evidence", ""), source_text):
            kept.append(a)
        else:
            dropped.append(a.get("title", "(untitled)"))

    diff_ev = analysis.get("difficulty_evidence", "")
    diff_grounded = _quote_is_grounded(diff_ev, source_text)

    analysis["assignments"] = kept
    analysis["grounding"] = {
        "assignments_kept": len(kept),
        "assignments_dropped": dropped,
        "difficulty_grounded": diff_grounded,
    }

    # If the difficulty rating isn't backed by the text, don't trust it —
    # fall back to neutral rather than letting an unsupported number shape
    # the schedule.
    if not diff_grounded:
        analysis["difficulty"] = 3
        analysis["difficulty_reason"] = (
            "Ungrounded rating discarded; defaulted to neutral."
        )
        analysis["weekly_hours_estimate"] = 0

    return analysis


# ---------------------------------------------------------------------------
# Feed verified syllabus assignments into the scheduler
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_due_hint(hint: str, year: int | None = None):
    """Turn a due_hint like 'December 15' or '10/9' into a datetime.

    Returns None for vague hints ('weekly', 'unknown') — those are real work
    but have no single deadline, so they aren't schedulable as one block.
    """
    import datetime as _dt

    if not hint:
        return None
    h = hint.strip().lower()
    if h in ("weekly", "unknown", "ongoing", "n/a", ""):
        return None

    now = _dt.datetime.now(_dt.timezone.utc)
    year = year or now.year

    # "December 15" / "Dec 15" / "15 December"
    m = re.search(r"([a-z]+)\s+(\d{1,2})", h)
    if m and m.group(1) in _MONTHS:
        month, day = _MONTHS[m.group(1)], int(m.group(2))
    else:
        m = re.search(r"(\d{1,2})\s+([a-z]+)", h)
        if m and m.group(2) in _MONTHS:
            day, month = int(m.group(1)), _MONTHS[m.group(2)]
        else:
            # numeric like 10/9 or 12-15
            m = re.search(r"(\d{1,2})[/-](\d{1,2})", h)
            if not m:
                return None
            month, day = int(m.group(1)), int(m.group(2))

    try:
        due = _dt.datetime(year, month, day, 23, 59, tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    # If the date already passed this year, assume it means next year.
    if due < now:
        try:
            due = due.replace(year=year + 1)
        except ValueError:
            return None
    return due


def syllabus_tasks() -> list:
    """Build Task objects from VERIFIED syllabus assignments with real dates.

    Only assignments that survived grounding verification AND have a parseable
    deadline become tasks. Recurring work ('weekly') is excluded here — it has
    no single due date, so it can't be scheduled as one block.
    """
    from .models import Task

    if not SYLLABUS_CACHE.exists():
        return []
    try:
        data = json.loads(SYLLABUS_CACHE.read_text())
    except Exception:
        return []

    tasks = []
    for course, analysis in data.items():
        for a in analysis.get("assignments", []) or []:
            due = _parse_due_hint(a.get("due_hint", ""))
            if due is None:
                continue
            title = a.get("title", "").strip()
            if not title:
                continue
            tasks.append(
                Task(
                    source="syllabus",
                    source_ref=f"{analysis.get('course_id','?')}:{title[:40]}",
                    title=title,
                    course=course,
                    description=a.get("evidence", "")[:500] or None,
                    due_at=due,
                    points_possible=None,
                    course_total_points=None,
                )
            )
    return tasks


def recurring_work() -> list[dict]:
    """Verified syllabus work with no single deadline (weekly readings etc.).

    Not schedulable as a block, but worth surfacing in the briefing so the
    student knows it exists.
    """
    if not SYLLABUS_CACHE.exists():
        return []
    try:
        data = json.loads(SYLLABUS_CACHE.read_text())
    except Exception:
        return []
    out = []
    for course, analysis in data.items():
        for a in analysis.get("assignments", []) or []:
            if _parse_due_hint(a.get("due_hint", "")) is None:
                out.append({
                    "course": course,
                    "title": a.get("title", ""),
                    "cadence": a.get("due_hint", "ongoing"),
                    "type": a.get("type", "other"),
                })
    return out
