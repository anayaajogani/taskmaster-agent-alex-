"""Calendar data for the landing page.

The task list answers "what should I do"; the calendar answers "when". Both
read from the same source so the colors and priorities always agree — the
calendar is not a separate view of separate data.

Exports:
  - work blocks the scheduler placed on the Taskmaster calendar
  - deadline markers for every task, so due dates show up alongside the work

Colors match taskmaster_calendar.py exactly (same tiers, same meaning).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent
CALENDAR_JSON = _AGENT_ROOT / "calendar_view.json"

# Same tiers as the Google Calendar colorIds, expressed as names the UI maps
# to CSS. Keeping one definition means the page and the calendar can't drift.
TIER_CRITICAL = "critical"   # due within 2 days      (red)
TIER_SOON = "soon"           # due within 5 days      (orange)
TIER_UPCOMING = "upcoming"   # due within 2 weeks     (yellow)
TIER_LATER = "later"         # further out            (green)
TIER_PRIORITY = "priority"   # flagged priority course (purple)


def tier_for(due: dt.datetime, is_priority_course: bool = False) -> str:
    """Identical logic to _pick_color() in taskmaster_calendar.py."""
    if is_priority_course:
        return TIER_PRIORITY
    days_out = (due - dt.datetime.now().astimezone()).total_seconds() / 86400
    if days_out <= 2:
        return TIER_CRITICAL
    if days_out <= 5:
        return TIER_SOON
    if days_out <= 14:
        return TIER_UPCOMING
    return TIER_LATER


def build_calendar_view(weeks_ahead: int = 3) -> dict:
    """Read the Taskmaster calendar's blocks + task deadlines into one payload.

    Falls back gracefully if calendar access isn't set up — the page still
    renders deadlines from the task data alone.
    """
    events: list[dict] = []
    deadlines: list[dict] = []

    # ---- deadlines, straight from the task list (always available) ----
    try:
        task_data = json.loads((_AGENT_ROOT / "task_list.json").read_text())
        for t in task_data.get("tasks", []):
            due_raw = t.get("due")
            if not due_raw:
                continue
            deadlines.append({
                "title": t.get("title"),
                "course": t.get("course"),
                "due_label": due_raw,
                "hours": t.get("budgeted_hours"),
                "from_syllabus": t.get("from_syllabus", False),
                "priority_course": t.get("priority_course", False),
            })
    except Exception:
        pass

    # ---- work blocks from the Taskmaster calendar ----
    try:
        from .taskmaster_calendar import (
            _get_service, _get_or_create_calendar, CALENDAR_NAME, TOKEN_FILE
        )
        import os
        if os.path.exists(TOKEN_FILE):
            service = _get_service()
            cal_id = _get_or_create_calendar(service)
            now = dt.datetime.now(dt.timezone.utc)
            end = now + dt.timedelta(weeks=weeks_ahead)
            page = None
            while True:
                resp = service.events().list(
                    calendarId=cal_id,
                    timeMin=now.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    pageToken=page,
                    maxResults=250,
                ).execute()
                for ev in resp.get("items", []):
                    start = (ev.get("start") or {}).get("dateTime")
                    finish = (ev.get("end") or {}).get("dateTime")
                    if not start or not finish:
                        continue
                    events.append({
                        "title": ev.get("summary", ""),
                        "start": start,
                        "end": finish,
                        "description": ev.get("description", ""),
                        "color_id": ev.get("colorId", "5"),
                    })
                page = resp.get("nextPageToken")
                if not page:
                    break
    except Exception:
        pass  # no calendar access yet; deadlines still render

    payload = {
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "calendar_name": "Taskmaster",
        "events": events,
        "deadlines": deadlines,
        "has_calendar_access": bool(events),
    }
    CALENDAR_JSON.write_text(json.dumps(payload, indent=2, default=str))
    return payload


if __name__ == "__main__":
    data = build_calendar_view()
    print(f"\n  {len(data['events'])} work block(s), "
          f"{len(data['deadlines'])} deadline(s)")
    print(f"  Written to {CALENDAR_JSON.name}\n")
    for e in data["events"][:8]:
        print(f"    {e['start'][:16].replace('T',' ')}  {e['title'][:50]}")
