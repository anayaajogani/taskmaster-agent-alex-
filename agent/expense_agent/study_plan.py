"""Today's study plan.

Deadline work is only part of a day. This decides what to actually do with
the rest of your stated daily capacity, drawing from open course materials.

How it decides:
  1. Deadline work comes first — that's what's actually due.
  2. Whatever capacity is left gets filled with materials.
  3. Materials are ordered by how soon that course's next deadline is, then
     by whether you flagged the course as a priority, then by type
     (quizzes and assignments before background reading).
  4. It stops once the day is full. A short list beats an overwhelming one.

On time estimates — being straight about this: Canvas gives a title and a
type, not a length. So per-item minutes are ROUGH DEFAULTS by type, not
measured or model-guessed values. They're labelled as estimates everywhere
they appear, and you can edit MINUTES_BY_TYPE to match your own pace.
"""

from __future__ import annotations

import datetime as dt

# Rough defaults, in minutes. Not measured — a starting point you can tune.
MINUTES_BY_TYPE = {
    "Quiz": 20,
    "Assignment": 45,
    "Page": 25,
    "Discussion": 30,
    "File": 30,
    "ExternalUrl": 25,
    "ExternalTool": 25,
}
DEFAULT_MINUTES = 30

# Do the things that are graded or gate progress before background reading.
TYPE_ORDER = {
    "Quiz": 0,
    "Assignment": 1,
    "Discussion": 2,
    "Page": 3,
    "File": 4,
    "ExternalUrl": 5,
    "ExternalTool": 5,
}


def _minutes_for(item: dt) -> int:
    return MINUTES_BY_TYPE.get(item.get("type", ""), DEFAULT_MINUTES)


def _course_urgency(course: str, active: list, upcoming: list) -> int:
    """Days until this course's next deadline. Far future if it has none."""
    best = 9999
    for t in list(active) + list(upcoming):
        if (t.get("course") or "") == course:
            d = t.get("days_left")
            if isinstance(d, int) and d < best:
                best = d
    return best


def build_study_plan(view: dict, cfg: dict) -> dict:
    """Pick what to study today with the capacity deadline work leaves free."""
    cap_hours = cfg.get("daily_cap_hours", 4)
    active = view.get("active", [])
    upcoming = view.get("upcoming", [])
    materials = view.get("materials", [])

    # Deadline work claims capacity first. A task's budget is spread over the
    # days it has left, so today's share is what matters — not the whole task.
    deadline_minutes = 0.0
    for t in active:
        days = max(t.get("days_left", 1), 1)
        share = (t.get("hours", 0) * 60) / days
        deadline_minutes += share

    total_minutes = cap_hours * 60
    free_minutes = max(total_minutes - deadline_minutes, 0)

    priority_courses = [p.lower().strip() for p in cfg.get("priority_courses", []) if p.strip()]

    def sort_key(m):
        course = m.get("course", "")
        is_priority = any(p in course.lower() for p in priority_courses)
        return (
            _course_urgency(course, active, upcoming),   # soonest deadline first
            0 if is_priority else 1,                     # then priority courses
            TYPE_ORDER.get(m.get("type", ""), 9),        # then graded before reading
        )

    ranked = sorted(materials, key=sort_key)

    picked, spent = [], 0.0
    for m in ranked:
        mins = _minutes_for(m)
        if spent + mins > free_minutes:
            continue
        picked.append({**m, "est_minutes": mins})
        spent += mins

    return {
        "cap_hours": cap_hours,
        "deadline_minutes": round(deadline_minutes),
        "free_minutes": round(free_minutes),
        "planned_minutes": round(spent),
        "picks": picked,
        "not_today": max(len(materials) - len(picked), 0),
    }


def upcoming_modules(current_term: str = "Fall 2026") -> list[dict]:
    """Modules that aren't open yet, with unlock dates when Canvas gives them.

    Answers "what's coming next week" instead of silently hiding locked work.
    """
    from .canvas_poller import fetch_active_courses, _get, is_teaching_role
    from .onboarding import load_config

    cfg = load_config()
    excluded = [e.lower().strip() for e in cfg.get("excluded_courses", []) if e.strip()]
    out = []

    for course in fetch_active_courses():
        name = course.get("name") or ""
        if current_term and current_term not in name:
            continue
        if is_teaching_role(course):
            continue
        if any(e in name.lower() for e in excluded):
            continue
        try:
            mods = _get("/courses/" + str(course["id"]) + "/modules",
                        params={"per_page": 50})
        except Exception:
            continue
        for m in mods or []:
            state = (m.get("state") or "").lower()
            if state != "locked":
                continue
            unlock = m.get("unlock_at")
            when = None
            if unlock:
                try:
                    when = dt.datetime.fromisoformat(unlock.replace("Z", "+00:00"))
                except Exception:
                    when = None
            out.append({
                "course": name,
                "module": m.get("name", ""),
                "unlocks_at": when.isoformat() if when else None,
                "unlocks_in_days": (when.date() - dt.date.today()).days if when else None,
            })
    return out


def print_study_plan(plan: dict, DIM="\033[2m", RESET="\033[0m", ACC="\033[95m") -> None:
    picks = plan.get("picks", [])
    free_h = plan["free_minutes"] / 60
    plan_h = plan["planned_minutes"] / 60

    print(f"\n  {ACC}STUDY TODAY{RESET}  "
          f"{DIM}{round(free_h,1)}h free after deadline work · "
          f"planning {round(plan_h,1)}h{RESET}")

    if not picks:
        if plan["free_minutes"] < 15:
            print(f"    {DIM}Your day is already full with deadline work.{RESET}")
        else:
            print(f"    {DIM}No open materials to fill the time.{RESET}")
        return

    for p in picks:
        print(f"    · [{p['label']:<10}] {p['title'][:44]:<44} "
              f"{DIM}~{p['est_minutes']}m est{RESET}")
    if plan["not_today"]:
        print(f"    {DIM}+{plan['not_today']} more saved for another day{RESET}")


def print_upcoming_modules(mods: list[dict], DIM="\033[2m", RESET="\033[0m") -> None:
    if not mods:
        return
    print(f"\n  {DIM}OPENS SOON:{RESET}")
    for m in mods[:5]:
        if m["unlocks_in_days"] is not None:
            d = m["unlocks_in_days"]
            when = "tomorrow" if d == 1 else (f"in {d} days" if d > 0 else "now")
        else:
            when = "date not set"
        print(f"    {DIM}· {m['module'][:40]:<40} {m['course'][:24]:<24} {when}{RESET}")
