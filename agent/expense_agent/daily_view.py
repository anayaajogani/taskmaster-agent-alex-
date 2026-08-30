"""Daily task view — "what should I work on today?"

Separate from the calendar. The calendar answers *when* work is scheduled;
this answers *what is active right now*, ranked high to low.

A task becomes ACTIVE once its recommended start date arrives. That start
date comes from the student's onboarding lead-time answer, adjusted for how
big the task is:

    start_date = due_date - max(lead_time_days, hours_needed / daily_cap)

So a 12-hour project with a 4h/day cap opens at least 3 days out even if the
student said "1 to 2 days", because the math wouldn't fit otherwise.

Tiers come from the priority score the scheduler already computes, so they
reflect the student's stated priority_mode rather than a second opinion.

Run:
    uv run python -m expense_agent.daily_view
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import os as _os
_AGENT_ROOT = Path(_os.environ.get('STATE_DIR')) \
    if _os.environ.get('STATE_DIR') \
    else Path(__file__).resolve().parent.parent
DAILY_JSON = _AGENT_ROOT / "daily_view.json"

# ANSI colors for the terminal view
RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

TIER_HIGH = "HIGH"
TIER_MEDIUM = "MEDIUM"
TIER_LOW = "LOW"


def recommended_start(due: dt.datetime, hours_needed: float, cfg: dict) -> dt.datetime:
    """When should this task open up?

    Respects the student's stated lead time, but stretches it if the work
    physically cannot fit in that window at their daily cap.
    """
    lead = cfg.get("lead_time_days", 5)
    cap = max(cfg.get("daily_cap_hours", 4), 1)
    days_needed = hours_needed / cap
    # round up so partial days still get a full day of runway
    days_needed = int(days_needed) + (1 if days_needed % 1 else 0)
    window = max(lead, days_needed)
    return due - dt.timedelta(days=window)


def _tier(score: float, scores: list[float]) -> str:
    """Split tasks into three tiers by priority score.

    Uses the spread of today's actual scores rather than fixed thresholds, so
    the tiers stay meaningful whether you have 3 tasks or 30.
    """
    if not scores:
        return TIER_LOW
    hi = max(scores)
    lo = min(scores)
    if hi == lo:
        return TIER_HIGH
    pct = (score - lo) / (hi - lo)
    if pct >= 0.66:
        return TIER_HIGH
    if pct >= 0.33:
        return TIER_MEDIUM
    return TIER_LOW


def _spread_by_course(rows, per_course=3, total=15):
    """Cap the upcoming list without letting one busy course hide the others.

    A flat "nearest deadline first" cut meant two courses with weekly homework
    consumed every slot, so a midterm three weeks out simply vanished. Take a
    few from each course first, then fill any remaining space by urgency.
    """
    by_course = {}
    for r in rows:
        by_course.setdefault(r.get("course") or "?", []).append(r)

    picked, seen = [], set()
    for course_rows in by_course.values():
        for r in course_rows[:per_course]:
            picked.append(r)
            seen.add(id(r))

    for r in rows:                      # already ordered by start date
        if len(picked) >= total:
            break
        if id(r) not in seen:
            picked.append(r)
            seen.add(id(r))

    picked.sort(key=lambda r: r.get("opens_in_days", 999))
    return picked[:total]


def build_daily_view(briefing: list[dict], cfg: dict, tasks_raw=None, materials=None) -> dict:
    """Build today's view from the scheduler's briefing.

    Returns a dict with active tasks (tiered) and upcoming ones not yet open.
    """
    now = dt.datetime.now().astimezone()
    today = now.date()

    active, upcoming = [], []

    for b in briefing:
        # briefing stores due as a formatted string; prefer a raw datetime if
        # the caller passed the task objects alongside.
        due = b.get("_due_dt")
        if due is None:
            continue
        hours = b.get("budgeted_hours", 2.0)
        start = recommended_start(due, hours, cfg)

        days_left = (due.date() - today).days
        entry = {
            "title": b["title"],
            "course": b.get("course") or "",
            "due": b["due"],
            "due_date": due.date().isoformat(),
            "days_left": days_left,
            "start_date": start.date().isoformat(),
            "hours": hours,
            "score": b.get("rank") or 0,
            "from_syllabus": b.get("from_syllabus", False),
            "priority_course": b.get("priority_course", False),
            "work_type": b.get("work_type", "coursework"),
            "overdue_start": start.date() < today,
        }

        if start.date() <= today:
            active.append(entry)
        else:
            entry["opens_in_days"] = (start.date() - today).days
            upcoming.append(entry)

    scores = [a["score"] for a in active]
    for a in active:
        a["tier"] = _tier(a["score"], scores)

    active.sort(key=lambda x: -x["score"])
    upcoming.sort(key=lambda x: x["opens_in_days"])

    view = {
        "generated_at": now.isoformat(),
        "date": today.isoformat(),
        "daily_cap_hours": cfg.get("daily_cap_hours", 4),
        "active": active,
        "upcoming": _spread_by_course(upcoming, per_course=3, total=15),
        "materials": materials or [],
    }

    # What to actually study today with whatever capacity is left over.
    try:
        from .study_plan import build_study_plan
        view["study_plan"] = build_study_plan(view, cfg)
    except Exception:
        view["study_plan"] = None

    # Scheduled blocks + deadlines, so the page can draw a calendar that
    # matches the task list exactly.
    try:
        from .calendar_view import build_calendar_view
        view["calendar"] = build_calendar_view()
    except Exception:
        view["calendar"] = None

    # Every current course, including quiet ones — so a course with nothing
    # posted reads as "nothing posted yet" rather than looking forgotten.
    try:
        from .canvas_poller import course_roster
        view["courses"] = course_roster()
    except Exception:
        view["courses"] = []

    # Syllabi the student dropped in, and course website URLs.
    try:
        from .manual_sources import build_manual_index
        view["manual"] = build_manual_index()
    except Exception:
        view["manual"] = None

    DAILY_JSON.write_text(json.dumps(view, indent=2, default=str))
    return view


def print_daily_view(view: dict) -> None:
    date_str = dt.date.fromisoformat(view["date"]).strftime("%A, %B %d")
    active = view["active"]
    cap = view["daily_cap_hours"]

    print("\n" + "=" * 74)
    print(f"  {BOLD}TODAY — {date_str}{RESET}")
    print("=" * 74)

    if not active:
        print("\n  Nothing active yet. Enjoy it while it lasts.\n")
    else:
        planned = 0.0
        for tier, color, label in (
            (TIER_HIGH, RED, "HIGH PRIORITY"),
            (TIER_MEDIUM, YELLOW, "MEDIUM PRIORITY"),
            (TIER_LOW, GREEN, "LOW PRIORITY"),
        ):
            rows = [a for a in active if a["tier"] == tier]
            if not rows:
                continue
            print(f"\n  {color}{BOLD}{label}{RESET}")
            for a in rows:
                marks = ""
                if a["priority_course"]:
                    marks += "*"
                if a["from_syllabus"]:
                    marks += "S"
                late = f" {RED}(should have started){RESET}" if a["overdue_start"] else ""
                days = a["days_left"]
                due_txt = "due TODAY" if days == 0 else (
                    "due tomorrow" if days == 1 else f"{days} days left"
                )
                print(f"    {color}•{RESET} {a['title'][:40]:<40} "
                      f"{DIM}{a['course'][:18]:<18}{RESET} "
                      f"{due_txt:<14} {a['hours']}h {marks}{late}")
                planned += a["hours"]

        print(f"\n  {DIM}{len(active)} active · ~{round(planned,1)}h of work open · "
              f"cap {cap}h/day{RESET}")
        if planned > cap:
            print(f"  {YELLOW}More open work than one day allows — the calendar "
                  f"spreads it out.{RESET}")

    upcoming = view.get("upcoming", [])
    if upcoming:
        print(f"\n  {DIM}NOT YET — opens later:{RESET}")
        for u in upcoming[:5]:
            d = u["opens_in_days"]
            when = "tomorrow" if d == 1 else f"in {d} days"
            print(f"    {DIM}· {u['title'][:38]:<38} start {when}{RESET}")

    plan = view.get("study_plan")
    if plan:
        try:
            from .study_plan import print_study_plan
            print_study_plan(plan, DIM=DIM, RESET=RESET, ACC=BOLD)
        except Exception:
            pass

    mats = view.get("materials", [])
    picked_titles = {p["title"] for p in (plan or {}).get("picks", [])}
    rest = [m for m in mats if m["title"] not in picked_titles]
    if rest:
        print(f"\n  {DIM}ALSO OPEN ({len(rest)}):{RESET}")
        for r in rest[:6]:
            print(f"      {DIM}· [{r['label']:<10}] {r['title'][:46]}{RESET}")

    print("\n" + "=" * 74)
    print(f"  {DIM}* priority course   S from syllabus   ~est = rough guide, not measured{RESET}")
    print("=" * 74 + "\n")


if __name__ == "__main__":
    # Standalone run: pull tasks, score them, show today's view.
    # Does NOT touch the calendar — this is the read-only daily view.
    from .taskmaster_calendar import (
        _budget_hours, _is_excluded, _is_priority_course, _rank_value, _dedupe_key,
    )
    from .canvas_poller import assignments_to_tasks
    from .onboarding import load_config

    try:
        from .syllabus import syllabus_tasks
    except Exception:
        def syllabus_tasks():
            return []

    cfg = load_config()
    canvas = assignments_to_tasks()
    seen = {_dedupe_key(t) for t in canvas}
    extra = [t for t in syllabus_tasks() if _dedupe_key(t) not in seen]
    tasks = [t for t in (canvas + extra) if not _is_excluded(t, cfg)]

    briefing = []
    for t in tasks:
        t.priority_score = _rank_value(t, cfg)
        briefing.append({
            "title": t.title,
            "course": t.course,
            "due": f"{t.due_at.astimezone():%a %b %d %I:%M %p}",
            "_due_dt": t.due_at.astimezone(),
            "rank": t.priority_score,
            "budgeted_hours": _budget_hours(t, cfg),
            "from_syllabus": t.source == "syllabus",
            "priority_course": _is_priority_course(t, cfg),
            "work_type": getattr(t, "work_type", "coursework"),
        })

    try:
        from .materials import fetch_materials
        materials = fetch_materials()
    except Exception:
        materials = []

    print_daily_view(build_daily_view(briefing, cfg, materials=materials))
