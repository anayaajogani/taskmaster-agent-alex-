"""The ADK graph's one consequential action: a real Google Calendar write.

Both routes out of ``estimate_and_score`` (QUIET and HIGH_PRIORITY) call
``schedule_block`` so every task the agent decides on produces a visible
calendar change — that's the "2-3 consequential tool actions" the Taskmaster
track is judged on.

Deliberately thin: it reuses ``taskmaster_calendar``'s auth, calendar
lookup, work-hour rules, and color logic rather than standing up a second
scheduler. Whether blocks land on a new dedicated calendar or the
student's primary one is a single config choice
(``calendar_target`` in ``taskmaster_config.json``, set via
``onboarding.py``), not two competing files — this is safe here because
``schedule_block`` only ever inserts or patches the one event it owns
(via ``source_ref``), never wipes anything. ``taskmaster_calendar.py``'s
local scheduler always uses the dedicated calendar regardless of this
setting, because it wipes and rebuilds its calendar's *entire* future on
every run — doing that against ``primary`` would delete real events.

Idempotency: each event is stamped with
``extendedProperties.private.source_ref``. Re-processing the same
assignment (a retry, a re-run of the demo) patches the existing event
instead of creating a duplicate.

Conflict detection: the free-slot search always checks ``primary``'s real
busy times (in addition to the target calendar's own, if different), so a
newly added personal event is treated as a real conflict — not just the
agent's own prior blocks.
"""

from __future__ import annotations

import datetime as dt

from .models import Task
from .onboarding import load_config
from .taskmaster_calendar import (
    _advance_to_workable,
    _get_or_create_calendar,
    _get_service,
    _pick_color,
)

MAX_BLOCK_HOURS = 3.0


def _find_free_slot(service, cal_id: str, cfg: dict, hours: float, due: dt.datetime) -> dt.datetime:
    """Earliest free work-hours slot before ``due``.

    Checks ``primary`` (the student's real calendar — catches a newly added
    conflicting event) and, if blocks are being written somewhere else
    (``cal_id``, e.g. the dedicated Taskmaster calendar), that calendar's
    own busy times too, so the agent doesn't double-book against blocks it
    already made itself. If ``cal_id`` already is ``primary`` there's
    nothing to add.
    """
    now = dt.datetime.now().astimezone()
    duration = dt.timedelta(hours=hours)

    query_ids = {"primary", cal_id}
    try:
        fb = service.freebusy().query(body={
            "timeMin": now.isoformat(),
            "timeMax": due.isoformat(),
            "items": [{"id": qid} for qid in query_ids],
        }).execute()
        busy = [
            period
            for qid in query_ids
            for period in fb["calendars"].get(qid, {}).get("busy", [])
        ]
    except Exception:
        busy = []

    cursor = _advance_to_workable(now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1), cfg)
    guard = 0
    while cursor + duration <= due and guard < 500:
        guard += 1
        end_h = cfg.get("work_day_end", 21)
        if cursor.hour + hours <= end_h:
            slot_end = cursor + duration
            overlaps = any(
                not (slot_end <= dt.datetime.fromisoformat(b["start"]) or
                     cursor >= dt.datetime.fromisoformat(b["end"]))
                for b in busy
            )
            if not overlaps:
                return cursor
        cursor = _advance_to_workable(cursor + dt.timedelta(hours=1), cfg)

    # Fallback: the morning before the deadline, work-hours rules be damned —
    # better a visibly-tight block than silently no block.
    start_h = cfg.get("work_day_start", 9)
    return (due - dt.timedelta(days=1)).replace(hour=start_h, minute=0, second=0, microsecond=0)


def schedule_block(
    title: str,
    course: str,
    due_at: str,
    estimated_hours: float,
    source_ref: str = "",
    priority_score: float = 0.0,
) -> dict:
    """Create or update a work-block for one task.

    Writes to the student's primary calendar or a new dedicated
    "Taskmaster" calendar depending on ``calendar_target`` in
    ``taskmaster_config.json`` (default: dedicated). Set via
    ``onboarding.py``.

    Args:
        title: Assignment title.
        course: Course name.
        due_at: Due datetime as ISO string.
        estimated_hours: LLM-estimated effort in hours.
        source_ref: Stable id from the source (e.g. Canvas assignment id).
            Used as the idempotency key so re-runs patch, not duplicate.
        priority_score: The deterministic priority score, for the event
            description and color.

    Returns:
        Dict with status ("scheduled" | "updated" | "error") and the link.
    """
    try:
        due = dt.datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except Exception:
        return {"status": "error", "detail": f"bad due_at: {due_at}"}

    hours = max(0.5, min(float(estimated_hours or 2), MAX_BLOCK_HOURS))
    cfg = load_config()

    task = Task(
        source="canvas",
        source_ref=source_ref,
        title=title,
        course=course,
        due_at=due,
        priority_score=priority_score,
    )

    try:
        service = _get_service()
        cal_id = "primary" if cfg.get("calendar_target") == "primary" else _get_or_create_calendar(service)

        existing_id = None
        if source_ref:
            existing = service.events().list(
                calendarId=cal_id,
                privateExtendedProperty=f"source_ref={source_ref}",
                maxResults=1,
            ).execute()
            items = existing.get("items", [])
            existing_id = items[0]["id"] if items else None

        start = _find_free_slot(service, cal_id, cfg, hours, due)
        end = start + dt.timedelta(hours=hours)
        due_local = due.astimezone()

        body = {
            "summary": f"Work: {title} ({course})",
            "description": (
                f"Auto-scheduled by Taskmaster. Priority {priority_score}. "
                f"Due {due_local:%a %b %d %I:%M %p}."
            ),
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "colorId": _pick_color(task, cfg, due_local),
            "extendedProperties": {"private": {"source_ref": source_ref}},
        }

        if existing_id:
            updated = service.events().patch(
                calendarId=cal_id, eventId=existing_id, body=body
            ).execute()
            return {"status": "updated", "title": title, "link": updated.get("htmlLink", "")}

        created = service.events().insert(calendarId=cal_id, body=body).execute()
        return {"status": "scheduled", "title": title, "link": created.get("htmlLink", "")}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:200]}
