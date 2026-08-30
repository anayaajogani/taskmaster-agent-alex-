"""Dedicated Taskmaster calendar, driven by the onboarding config.

Creates and owns a separate "Taskmaster" calendar, then wipes and rebuilds its
work blocks each run (safe reshuffle — it owns the calendar, so it never
touches your real events).

Every onboarding answer changes real behavior here:
  priority_mode      -> which factor dominates the ranking
  lead_time_days     -> how early work starts before a deadline
  work_day_start/end -> quiet hours, never scheduled outside them
  off_days           -> full days kept clear
  daily_cap_hours    -> max hours scheduled on any one day
  priority_courses   -> those courses get boosted
  excluded_courses   -> ignored entirely (e.g. classes you tutor)
  effort_padding     -> how much to inflate effort estimates

Run:
    uv run python -m expense_agent.taskmaster_calendar
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections import defaultdict
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .canvas_poller import assignments_to_tasks
from .onboarding import load_config
from .task_list import write_task_list

try:
    from .syllabus import difficulty_multipliers, syllabus_tasks, recurring_work
except Exception:  # syllabus analysis is optional
    def difficulty_multipliers() -> dict:
        return {}

    def syllabus_tasks() -> list:
        return []

    def recurring_work() -> list:
        return []

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_AGENT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = os.environ.get("GCAL_CREDENTIALS", str(_AGENT_ROOT / "gcal_credentials.json"))
TOKEN_FILE = os.environ.get("GCAL_TOKEN", str(_AGENT_ROOT / "gcal_token.json"))

CALENDAR_NAME = "Taskmaster"
MAX_BLOCK_HOURS = 3.0
PRIORITY_COURSE_BOOST = 1.5

# Google Calendar colorIds (what the numbers actually look like):
#   11 Tomato (red)   6 Tangerine (orange)   5 Banana (yellow)
#   10 Basil (green)  9 Blueberry (blue)     3 Grape (purple)
COLOR_CRITICAL = "11"   # red    - due within 2 days
COLOR_SOON = "6"        # orange - due within 5 days
COLOR_UPCOMING = "5"    # yellow - due within 2 weeks
COLOR_LATER = "10"      # green  - further out
COLOR_PRIORITY = "3"    # purple - a course the student flagged as priority


COLOR_TEACHING = "9"    # blueberry - work you owe students, not graded work


def _pick_color(task, cfg, due_local: dt.datetime) -> str:
    """Color-code blocks so the calendar is readable at a glance.

    Priority courses get their own color; everything else is colored by how
    soon it's due, so red always means 'this is the fire'.
    """
    if getattr(task, "work_type", "coursework") == "teaching":
        return COLOR_TEACHING
    if _is_priority_course(task, cfg):
        return COLOR_PRIORITY
    days_out = (due_local - dt.datetime.now().astimezone()).total_seconds() / 86400
    if days_out <= 2:
        return COLOR_CRITICAL
    if days_out <= 5:
        return COLOR_SOON
    if days_out <= 14:
        return COLOR_UPCOMING
    return COLOR_LATER


def _consent_prompt() -> bool:
    """Explain what we're about to do and let the user decline.

    An agent that writes to your calendar should say so plainly before it asks
    for access, not just throw an OAuth window at you.
    """
    if os.path.exists(TOKEN_FILE):
        return True  # already authorized; don't nag on every run

    print("\n" + "=" * 70)
    print("  CALENDAR ACCESS")
    print("=" * 70)
    print("""
  To schedule your work, I need permission to use Google Calendar.

  What I will do:
    - Create a SEPARATE calendar called "Taskmaster"
    - Put work blocks only on that calendar
    - Rebuild those blocks when your priorities change

  What I will NOT do:
    - Touch, move, or delete anything on your existing calendars
    - Read your personal events

  You can hide or delete the Taskmaster calendar at any time, and
  nothing else in your account is affected.
""")
    print("=" * 70)
    answer = input("  Allow calendar access? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("""
  No problem — skipping calendar scheduling.

  NOTE: Without calendar access I can still rank your tasks and write
  your task list (TASK_LIST.md). If you change your mind, just run this
  again and answer yes. I would only ever create my own separate
  calendar; your real ones stay untouched.
""")
        return False
    return True


def _get_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def _get_or_create_calendar(service) -> str:
    page_token = None
    while True:
        cal_list = service.calendarList().list(pageToken=page_token).execute()
        for entry in cal_list["items"]:
            if entry.get("summary") == CALENDAR_NAME:
                return entry["id"]
        page_token = cal_list.get("nextPageToken")
        if not page_token:
            break
    created = service.calendars().insert(
        body={"summary": CALENDAR_NAME, "timeZone": "America/Los_Angeles"}
    ).execute()
    return created["id"]


def _clear_calendar(service, cal_id: str) -> None:
    """Delete all future events on OUR calendar only."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    page_token = None
    while True:
        events = service.events().list(
            calendarId=cal_id, timeMin=now, pageToken=page_token, maxResults=250
        ).execute()
        for ev in events.get("items", []):
            try:
                service.events().delete(calendarId=cal_id, eventId=ev["id"]).execute()
            except Exception:
                pass
        page_token = events.get("nextPageToken")
        if not page_token:
            break


def _is_excluded(task, cfg) -> bool:
    """Skip courses the student told us to ignore (e.g. ones they tutor)."""
    course = (task.course or "").lower()
    title = (task.title or "").lower()
    for ex in cfg.get("excluded_courses", []):
        e = ex.lower().strip()
        if e and (e in course or e in title):
            return True
    return False


def _is_priority_course(task, cfg) -> bool:
    course = (task.course or "").lower()
    for p in cfg.get("priority_courses", []):
        pl = p.lower().strip()
        if pl and pl in course:
            return True
    return False


def _rank_value(task, cfg) -> float:
    """Ranking driven by the student's stated priority_mode.

    Teaching work (grading, prep) is ranked on urgency alone — "worth more of
    my grade" is meaningless for a course you TA, so applying the student's
    grade-weighting to it would be nonsense.
    """
    now = dt.datetime.now(dt.timezone.utc)
    hours_left = max((task.due_at - now).total_seconds() / 3600, 1.0)
    urgency = 24.0 / hours_left

    if getattr(task, "work_type", "coursework") == "teaching":
        # Deadlines you owe other people are real, but they aren't graded work.
        return round(urgency * 1.5, 4)

    if task.points_possible and task.course_total_points:
        grade = task.points_possible / task.course_total_points
    else:
        grade = 0.15
    effort = task.estimated_hours or 2.0

    mode = cfg.get("priority_mode", "grade")
    if mode == "grade":
        value = urgency * (1 + 3 * grade)
    elif mode == "urgency":
        value = urgency * 2
    elif mode == "effort":
        value = urgency * (1 + effort / 4)
    else:
        value = urgency * (1 + effort / 3) * (1 + grade)

    if _is_priority_course(task, cfg):
        value *= PRIORITY_COURSE_BOOST
    return round(value, 4)


def _budget_hours(task, cfg) -> float:
    base = task.estimated_hours or 2.0
    if task.points_possible and task.course_total_points:
        weight = task.points_possible / task.course_total_points
    else:
        weight = 0.1
    grade_factor = 1.0 + min(weight, 0.5)
    padding = cfg.get("effort_padding", 1.2)
    total = base * grade_factor * padding

    # Course difficulty learned from the syllabus (if analysis has been run).
    mults = difficulty_multipliers()
    course = task.course or ""
    for name, mult in mults.items():
        if name and (name in course or course in name):
            total *= mult
            break

    if _is_priority_course(task, cfg):
        total *= 1.1
    return round(total, 1)


def _advance_to_workable(cursor: dt.datetime, cfg) -> dt.datetime:
    """Move the cursor forward until it lands inside allowed work time."""
    start_h = cfg.get("work_day_start", 9)
    end_h = cfg.get("work_day_end", 21)
    off_days = [d[:3].title() for d in cfg.get("off_days", [])]

    for _ in range(24 * 60):
        if cursor.strftime("%a") in off_days:
            cursor = (cursor + dt.timedelta(days=1)).replace(hour=start_h, minute=0)
            continue
        if cursor.hour < start_h:
            cursor = cursor.replace(hour=start_h, minute=0)
            continue
        if cursor.hour >= end_h:
            cursor = (cursor + dt.timedelta(days=1)).replace(hour=start_h, minute=0)
            continue
        return cursor
    return cursor


def _place_blocks(service, cal_id, tasks_sorted, cfg) -> list[dict]:
    briefing = []
    lead = dt.timedelta(days=cfg.get("lead_time_days", 5))
    cap = cfg.get("daily_cap_hours", 4)
    start_h = cfg.get("work_day_start", 9)
    end_h = cfg.get("work_day_end", 21)
    per_day: dict = defaultdict(float)

    now_cursor = dt.datetime.now().astimezone().replace(
        minute=0, second=0, microsecond=0
    ) + dt.timedelta(hours=1)

    for task in tasks_sorted:
        total = _budget_hours(task, cfg)
        remaining = total
        due_local = task.due_at.astimezone()

        earliest = max(
            now_cursor,
            min(due_local - lead, due_local - dt.timedelta(hours=total)),
        )
        block_cursor = _advance_to_workable(max(now_cursor, earliest), cfg)

        placed = 0
        guard = 0
        while remaining > 0 and guard < 300:
            guard += 1
            block_cursor = _advance_to_workable(block_cursor, cfg)
            if block_cursor >= due_local:
                break
            day_key = block_cursor.date()
            room_today = cap - per_day[day_key]
            if room_today <= 0:
                block_cursor = _advance_to_workable(
                    (block_cursor + dt.timedelta(days=1)).replace(hour=start_h, minute=0),
                    cfg,
                )
                continue
            chunk = min(remaining, MAX_BLOCK_HOURS, room_today, end_h - block_cursor.hour)
            if chunk <= 0:
                block_cursor = _advance_to_workable(
                    (block_cursor + dt.timedelta(days=1)).replace(hour=start_h, minute=0),
                    cfg,
                )
                continue
            start = block_cursor
            end = start + dt.timedelta(hours=chunk)
            service.events().insert(calendarId=cal_id, body={
                "summary": f"Work: {task.title} ({task.course})",
                "description": (
                    f"Auto-scheduled by Taskmaster. Rank {task.priority_score}. "
                    f"Due {due_local:%a %b %d %I:%M %p}."
                ),
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
                "colorId": _pick_color(task, cfg, due_local),
            }).execute()
            per_day[day_key] += chunk
            remaining -= chunk
            placed += 1
            block_cursor = end

        briefing.append({
            "title": task.title,
            "course": task.course,
            "due": f"{due_local:%a %b %d %I:%M %p}",
            "rank": task.priority_score,
            "budgeted_hours": total,
            "blocks": placed,
            "fully_scheduled": remaining <= 0,
            "priority_course": _is_priority_course(task, cfg),
            "from_syllabus": task.source == "syllabus",
            "work_type": getattr(task, "work_type", "coursework"),
        })
    return briefing


def _dedupe_key(task) -> str:
    """Collapse a Canvas assignment and its syllabus mention into one task.

    Keyed on title + course, NOT the due date: the syllabus often gives a
    slightly different time than Canvas (4:59pm vs 7:00pm for the same exam),
    and two assignments with the same name in the same course are the same
    thing regardless.
    """
    title = re.sub(r"[^a-z0-9]+", " ", (task.title or "").lower()).strip()
    course = re.sub(r"[^a-z0-9]+", " ", (task.course or "").lower()).strip()[:24]
    return f"{title}|{course}"


def rebuild_calendar_and_brief():
    cfg = load_config()
    canvas_tasks = assignments_to_tasks()

    # Courses Canvas says you TA/teach were already dropped at the source.
    auto_skipped = list(getattr(assignments_to_tasks, "last_skipped_teaching", []))

    # Merge in verified syllabus assignments Canvas never listed (finals,
    # project milestones). Canvas wins on conflicts since it's authoritative.
    seen = {_dedupe_key(t) for t in canvas_tasks}
    from_syllabus = []
    for t in syllabus_tasks():
        k = _dedupe_key(t)
        if k not in seen:
            seen.add(k)
            from_syllabus.append(t)

    all_tasks = canvas_tasks + from_syllabus

    kept, skipped = [], []
    for t in all_tasks:
        if _is_excluded(t, cfg):
            skipped.append(f"{t.title} ({t.course})")
        else:
            kept.append(t)

    for t in kept:
        t.priority_score = _rank_value(t, cfg)
    kept.sort(key=lambda t: t.priority_score or 0, reverse=True)

    # Ask before touching the calendar. If declined, still rank and list tasks.
    if not _consent_prompt():
        briefing = [{
            "title": t.title,
            "course": t.course,
            "due": f"{t.due_at.astimezone():%a %b %d %I:%M %p}",
            "rank": t.priority_score,
            "budgeted_hours": _budget_hours(t, cfg),
            "blocks": 0,
            "fully_scheduled": False,
            "priority_course": _is_priority_course(t, cfg),
            "from_syllabus": t.source == "syllabus",
            "work_type": getattr(t, "work_type", "coursework"),
        } for t in kept]
        return briefing, skipped + auto_skipped, cfg

    service = _get_service()
    cal_id = _get_or_create_calendar(service)
    _clear_calendar(service, cal_id)
    briefing = _place_blocks(service, cal_id, kept, cfg)
    return briefing, skipped + auto_skipped, cfg


def print_briefing(briefing, skipped, cfg) -> None:
    print("\n" + "=" * 78)
    print("  TASK BRIEFING")
    line = (f"  Mode: {cfg['priority_mode']} | window {cfg['work_day_start']}:00-"
            f"{cfg['work_day_end']}:00 | max {cfg['daily_cap_hours']}h/day")
    if cfg.get("off_days"):
        line += f" | off: {','.join(cfg['off_days'])}"
    print(line)
    print("=" * 78)
    for i, b in enumerate(briefing, 1):
        flag = "OK   " if b["fully_scheduled"] else "TIGHT"
        star = "*" if b["priority_course"] else " "
        src = "T" if b.get("work_type") == "teaching" else ("S" if b.get("from_syllabus") else " ")
        print(f"{i:>2}.{star}{src}[{flag}] {b['title'][:32]:<32} | {(b['course'] or '')[:16]:<16} "
              f"| due {b['due']:<19} | {b['budgeted_hours']}h/{b['blocks']} blk")

    recurring = recurring_work()
    if recurring:
        print(f"\n  Ongoing work from syllabi ({len(recurring)}) — no single deadline,")
        print("  so not scheduled as blocks, but don't forget it:")
        by_course: dict = {}
        for r in recurring:
            by_course.setdefault(r["course"][:34], []).append(r["title"])
        for course, items in by_course.items():
            print(f"    {course}: {', '.join(items[:4])}")
    if skipped:
        print("\n  Ignored (you told me to skip these):")
        for s in skipped:
            print(f"    - {s}")
    if cfg.get("non_canvas_courses"):
        print(f"\n  Check manually (not on Canvas): {cfg['non_canvas_courses']}")
    print("\n  Calendar colors:  RED = due <2 days   ORANGE = <5 days   "
          "YELLOW = <2 weeks\n                    GREEN = later      "
          "PURPLE = priority course")
    print("  Markers: * = priority course   S = from syllabus   T = teaching work (you TA/tutor)")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    brief, skipped, cfg = rebuild_calendar_and_brief()
    print_briefing(brief, skipped, cfg)
    write_task_list(brief, skipped, cfg)
