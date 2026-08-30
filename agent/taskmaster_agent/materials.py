"""Course materials from Canvas Modules.

Deadlines aren't the whole story. Modules hold the readings, pages, quizzes and
files that make up week-to-week work but never appear as graded assignments
with due dates. This pulls the ones that are actually available to you and
that you haven't finished yet.

Respects the same rules as the rest of the agent:
  - courses where Canvas says you're a TA/teacher are skipped
  - courses in your onboarding exclusion list are skipped
  - locked modules are skipped (you can't do them yet)
  - items you've already completed are skipped

Run:
    uv run python -m taskmaster_agent.materials
"""

from __future__ import annotations

import json
from pathlib import Path

from .canvas_poller import fetch_active_courses, _get, is_teaching_role
from .onboarding import load_config

_AGENT_ROOT = Path(__file__).resolve().parent.parent
MATERIALS_JSON = _AGENT_ROOT / "materials.json"

# Item types worth surfacing as "things to do or read".
# SubHeader is a divider, not work. ExternalTool/ExternalUrl are usually links
# out to publisher sites — keep them, they're often the actual reading.
USEFUL_TYPES = {
    "Page", "Assignment", "Quiz", "Discussion", "File",
    "ExternalUrl", "ExternalTool",
}

# Friendlier labels than Canvas's internal names.
TYPE_LABEL = {
    "Page": "read",
    "Assignment": "assignment",
    "Quiz": "quiz",
    "Discussion": "discussion",
    "File": "file",
    "ExternalUrl": "link",
    "ExternalTool": "tool",
}


def _course_excluded(course_name: str, cfg: dict) -> bool:
    name = (course_name or "").lower()
    for ex in cfg.get("excluded_courses", []):
        e = ex.lower().strip()
        if e and e in name:
            return True
    return False


def _is_done(item: dict) -> bool:
    """Canvas marks completion when the module has requirements set."""
    req = item.get("completion_requirement") or {}
    return bool(req.get("completed"))


def fetch_materials(current_term: str = "Fall 2026") -> list[dict]:
    """Return unlocked, incomplete module items from courses you're taking."""
    cfg = load_config()
    out = []

    for course in fetch_active_courses():
        name = course.get("name") or ""
        if current_term and current_term not in name:
            continue
        if is_teaching_role(course):
            continue
        if _course_excluded(name, cfg):
            continue

        try:
            modules = _get(
                "/courses/" + str(course["id"]) + "/modules",
                params={"per_page": 50},
            )
        except Exception:
            continue

        for mod in modules or []:
            # Skip modules you can't open yet.
            if (mod.get("state") or "").lower() == "locked":
                continue
            try:
                items = _get(
                    "/courses/" + str(course["id"]) + "/modules/"
                    + str(mod["id"]) + "/items",
                    params={"per_page": 50},
                )
            except Exception:
                continue

            for it in items or []:
                itype = it.get("type", "")
                if itype not in USEFUL_TYPES:
                    continue
                if _is_done(it):
                    continue
                out.append({
                    "course": name,
                    "module": mod.get("name", ""),
                    "title": it.get("title", "Untitled"),
                    "type": itype,
                    "label": TYPE_LABEL.get(itype, itype.lower()),
                    "url": it.get("html_url", ""),
                })

    MATERIALS_JSON.write_text(json.dumps(
        {"materials": out}, indent=2
    ))
    return out


def print_materials(materials: list[dict]) -> None:
    if not materials:
        print("\n  No open course materials found.")
        print("  (Instructors may not have published modules yet.)\n")
        return

    print("\n" + "=" * 74)
    print("  COURSE MATERIALS — available now, not yet done")
    print("=" * 74)

    by_course: dict = {}
    for m in materials:
        by_course.setdefault(m["course"], []).append(m)

    for course, items in by_course.items():
        print(f"\n  {course[:66]}")
        by_mod: dict = {}
        for i in items:
            by_mod.setdefault(i["module"], []).append(i)
        for mod, rows in by_mod.items():
            print(f"    {mod[:60]}")
            for r in rows:
                print(f"      · [{r['label']:<10}] {r['title'][:50]}")

    print("\n" + "=" * 74)
    print(f"  {len(materials)} item(s) · saved to materials.json")
    print("=" * 74 + "\n")


if __name__ == "__main__":
    print("\nChecking Canvas modules for available materials...")
    print_materials(fetch_materials())
