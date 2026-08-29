"""Google Calendar integration for the taskmaster agent.

Provides `schedule_work_block(...)`: given a task's title, course, due date,
and estimated hours, it finds a free slot before the deadline and creates a
work-block event on the user's calendar.

First run opens a browser to authorize (one time). After that, a saved token
(gcal_token.json) is reused, so the agent can write to the calendar
autonomously with no further prompts.

Setup already done by the user:
  - Calendar API enabled in the Google Cloud project
  - OAuth desktop client downloaded to gcal_credentials.json (in agent/)
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Files live next to this module (inside expense_agent/), but credentials were
# copied to the agent/ root, so look one level up too.
_HERE = Path(__file__).resolve().parent
_AGENT_ROOT = _HERE.parent

CREDENTIALS_FILE = os.environ.get(
    "GCAL_CREDENTIALS",
    str(_AGENT_ROOT / "gcal_credentials.json"),
)
TOKEN_FILE = os.environ.get("GCAL_TOKEN", str(_AGENT_ROOT / "gcal_token.json"))

# Work-block placement rules (kept simple & transparent).
WORK_DAY_START = 9   # don't schedule before 9am
WORK_DAY_END = 21    # or after 9pm


def _get_service():
    """Return an authorized Calendar API service, refreshing/creating token."""
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


def _find_free_slot(service, duration_hours: float, due_at: dt.datetime) -> dt.datetime:
    """Find the earliest free start time before due_at, within work hours.

    Uses the freebusy API to avoid double-booking. Falls back to a default
    slot the day before the deadline if nothing obvious is free.
    """
    now = dt.datetime.now(dt.timezone.utc)
    window_start = now
    duration = dt.timedelta(hours=duration_hours)

    body = {
        "timeMin": window_start.isoformat(),
        "timeMax": due_at.isoformat(),
        "items": [{"id": "primary"}],
    }
    try:
        fb = service.freebusy().query(body=body).execute()
        busy = fb["calendars"]["primary"]["busy"]
    except Exception:
        busy = []

    # Walk hour by hour from now to due_at, pick first slot that fits in work
    # hours and doesn't overlap a busy block.
    cursor = window_start.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    while cursor + duration <= due_at:
        local_hour = cursor.astimezone().hour
        if WORK_DAY_START <= local_hour and (local_hour + duration_hours) <= WORK_DAY_END:
            slot_end = cursor + duration
            overlaps = any(
                not (slot_end <= dt.datetime.fromisoformat(b["start"]) or
                     cursor >= dt.datetime.fromisoformat(b["end"]))
                for b in busy
            )
            if not overlaps:
                return cursor
        cursor += dt.timedelta(hours=1)

    # Fallback: the morning before the deadline.
    return (due_at - dt.timedelta(days=1)).replace(hour=WORK_DAY_START, minute=0, second=0, microsecond=0)


def schedule_work_block(
    title: str,
    course: str,
    due_at: str,
    estimated_hours: float,
) -> dict:
    """Create a calendar work-block for a task before its due date.

    Args:
        title: Assignment title.
        course: Course name.
        due_at: Due datetime as ISO string.
        estimated_hours: How long the work block should be.

    Returns:
        Dict with the created event's link, or an error message.
    """
    try:
        due = dt.datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except Exception:
        return {"status": "error", "detail": f"bad due_at: {due_at}"}

    hours = max(0.5, min(float(estimated_hours or 2), 4))  # cap a single block at 4h

    try:
        service = _get_service()
        start = _find_free_slot(service, hours, due)
        end = start + dt.timedelta(hours=hours)

        event = {
            "summary": f"Work: {title} ({course})",
            "description": f"Auto-scheduled by taskmaster. Due {due_at}.",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "colorId": "5",  # yellow-ish, so app-created blocks are easy to spot
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        return {
            "status": "scheduled",
            "title": title,
            "start": start.isoformat(),
            "link": created.get("htmlLink", ""),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)[:200]}


if __name__ == "__main__":
    # Quick manual test: schedules a dummy block ~2 days out.
    demo_due = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    print(schedule_work_block("Test assignment", "DEMO 101", demo_due, 2))
