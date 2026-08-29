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

_AGENT_ROOT = Path(__file__).resolve().parent.parent
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


def gather_syllabus_text(course_id: int) -> tuple[str, str]:
    """Return (text, source_label)."""
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
  "weekly_hours_estimate": <number>,
  "assignments": [
    {"title": "<name>", "due_hint": "<date or 'weekly' or 'unknown'>", "type": "<reading|paper|exam|problem set|participation|project|other>"}
  ]
}

Rules:
- difficulty: 1 = very light, 5 = very demanding. Judge from workload,
  assessment weight, reading load, and stated expectations.
- weekly_hours_estimate: realistic out-of-class hours per week.
- assignments: list recurring or one-off work described in the syllabus
  (weekly readings, participation, papers, exams). Include things that would
  NOT appear in an assignments list, like "weekly reading response".
- Do NOT invent specifics not present in the text. If the syllabus is
  uninformative, return difficulty 3 and an empty assignments list.

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
        text, source = gather_syllabus_text(cid)
        analysis = analyze_with_gemini(text)
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
        if assignments:
            print(f"    Found in syllabus ({len(assignments)}):")
            for x in assignments[:8]:
                print(f"      - {x.get('title','')[:45]:<45} "
                      f"[{x.get('type','')}] due: {x.get('due_hint','')}")
    print("\n" + "=" * 78)
    print(f"  Saved to {SYLLABUS_CACHE.name}")
    print("  Difficulty multipliers now available to the scheduler.")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    print("\nAnalyzing syllabi for current courses...\n")
    res = analyze_all_courses()
    print_report(res)
