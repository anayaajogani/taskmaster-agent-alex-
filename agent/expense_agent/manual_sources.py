"""Manual course sources.

Two gaps Canvas can't fill:

  1. Most instructors never fill in the Canvas syllabus page, so the syllabus
     reader finds nothing. Drop the PDF in `syllabi/` and the agent reads it
     directly — same grounded extraction, real source text.

  2. Some courses (data science ones especially) run off their own website,
     not Canvas at all. We store the URL and surface it, but we do NOT pretend
     to scrape it: every course site is different and scraping them silently
     would produce made-up assignments. The link is one click away instead.

Usage:
    mkdir -p syllabi
    # drop Course-Name.pdf into syllabi/
    uv run python -m expense_agent.manual_sources

Filenames become course matches, so "Data-89.pdf" matches "Data 89".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import os as _os
_AGENT_ROOT = Path(_os.environ.get('STATE_DIR')) \
    if _os.environ.get('STATE_DIR') \
    else Path(__file__).resolve().parent.parent
SYLLABI_DIR = _AGENT_ROOT / "syllabi"
MANUAL_JSON = _AGENT_ROOT / "manual_sources.json"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def list_syllabus_files() -> list[dict]:
    """Every syllabus the student has dropped in, with the course it matches."""
    SYLLABI_DIR.mkdir(exist_ok=True)
    out = []
    for f in sorted(SYLLABI_DIR.iterdir()):
        if f.name.startswith(".") or f.is_dir():
            continue
        if f.suffix.lower() not in (".pdf", ".txt", ".md", ".docx"):
            continue
        out.append({
            "file": f.name,
            "course_hint": _slug(f.stem),
            "size_kb": round(f.stat().st_size / 1024),
            "readable": f.suffix.lower() in (".pdf", ".txt", ".md"),
        })
    return out


def extract_text(path: Path) -> str:
    """Pull text out of a dropped syllabus. Returns '' if it can't be read."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            return path.read_text(errors="ignore")[:20000]
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return ""  # pypdf not installed; caller reports this honestly
            reader = PdfReader(str(path))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
            return text[:20000]
    except Exception:
        return ""
    return ""


def match_course(course_name: str, files: list[dict]) -> dict | None:
    """Find a dropped syllabus that belongs to this course, if any."""
    cs = _slug(course_name)
    for f in files:
        hint = f["course_hint"]
        if not hint:
            continue
        # match either direction: "data 89" in the course name, or vice versa
        if hint in cs or cs.startswith(hint) or hint.startswith(cs[:16]):
            return f
    return None


def load_course_urls() -> dict:
    """Course websites the student entered (from onboarding or manual edit)."""
    try:
        return json.loads(MANUAL_JSON.read_text()).get("course_urls", {})
    except Exception:
        return {}


def save_course_url(course: str, url: str) -> None:
    data = {"course_urls": load_course_urls()}
    data["course_urls"][course] = url
    MANUAL_JSON.write_text(json.dumps(data, indent=2))


def build_manual_index() -> dict:
    """What the UI needs: which courses have a dropped syllabus, which have URLs."""
    files = list_syllabus_files()
    urls = load_course_urls()
    payload = {
        "syllabi_dir": str(SYLLABI_DIR),
        "files": files,
        "course_urls": urls,
        "pypdf_available": _pypdf_available(),
    }
    MANUAL_JSON.write_text(json.dumps(
        {"course_urls": urls, "files": files}, indent=2
    ))
    return payload


def _pypdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    SYLLABI_DIR.mkdir(exist_ok=True)
    idx = build_manual_index()

    print("\n" + "=" * 70)
    print("  MANUAL COURSE SOURCES")
    print("=" * 70)
    print(f"\n  Drop syllabus files here:\n    {SYLLABI_DIR}\n")

    if not idx["files"]:
        print("  No syllabus files yet.")
        print("  Save a PDF named after the course, e.g. 'Data-89.pdf'.")
    else:
        print(f"  {len(idx['files'])} file(s):")
        for f in idx["files"]:
            ok = "readable" if f["readable"] else "unsupported format"
            print(f"    - {f['file'][:44]:<44} {f['size_kb']:>5} KB  ({ok})")

    if not idx["pypdf_available"]:
        print("\n  NOTE: PDF reading needs pypdf. Install with:")
        print("        uv pip install pypdf")

    urls = idx["course_urls"]
    if urls:
        print(f"\n  Course websites ({len(urls)}):")
        for course, url in urls.items():
            print(f"    - {course[:34]:<34} {url[:40]}")
    else:
        print("\n  No course websites saved.")
        print("  Add one:  uv run python -c \"from expense_agent.manual_sources "
              "import save_course_url; save_course_url('Data 89', 'https://...')\"")
    print("\n" + "=" * 70 + "\n")
