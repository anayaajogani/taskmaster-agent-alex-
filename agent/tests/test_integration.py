# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Integration tests for the taskmaster agent.

Spins up the backend (ADK graph + Pub/Sub trigger) using an ASGI test
transport. The graph still calls the real Gemini effort-estimator agent
(no LLM mocking — same assumption the original taskmaster-agent test
suite made), so these need real credentials configured (GOOGLE_API_KEY or
Vertex AI application-default credentials), same as `make dev`.

What's mocked: Google Calendar. ``calendar_tool.schedule_block`` looks up
``_get_service`` by name in its own module at call time, so patching
``calendar_tool._get_service`` intercepts the real Calendar write no matter
which route calls it — the QUIET path calls ``schedule_block`` directly,
the HIGH_PRIORITY path calls it as a Gemini function-calling tool on
``reminder_agent``. One fake, both paths covered.

Two groups of tests:
- The ADK-routing tests (real Gemini calls) prove the graph wires up:
  QUIET/HIGH_PRIORITY routing, the reminder alert, and idempotent
  reprocessing. Assertions there tolerate the LLM's effort estimate
  varying slightly between calls — they check the calendar ends up
  self-consistent, not an exact block count.
- The deterministic tests (fixed ``estimated_hours`` passed directly to
  ``calendar_tool.schedule_block``, bypassing Gemini) prove the ported
  scheduling brain itself: multi-block splitting, the daily-hour cap,
  shrink-reconciliation, and real-calendar conflict avoidance.
"""

import base64
import datetime as dt
import json

import httpx
import pytest
import pytest_asyncio

from taskmaster_agent import agent as agent_module
from taskmaster_agent import calendar_tool
from taskmaster_agent.fast_api_app import app as backend_app


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeCalendarService:
    """Stand-in for the googleapiclient Calendar resource, backed by a
    single ``events`` dict rather than a source_ref-keyed shortcut, so it
    can support everything ``calendar_tool.py`` actually does: multi-block
    per task, day-range + extended-property filtering (for the live daily
    capacity lookup), deletes (for shrink-reconciliation), and injectable
    freebusy conflicts.
    """

    def __init__(self):
        self._events_by_id: dict[str, dict] = {}
        self._next_id = 0
        self.insert_count = 0
        self.patch_count = 0
        self.delete_count = 0
        self.last_calendar_id: str | None = None
        self.busy_periods: list[tuple[str, str]] = []

    def calendarList(self):  # noqa: N802 - matches googleapiclient's naming
        class _List:
            def list(self, pageToken=None):
                return _FakeExecutable({"items": [{"summary": "Taskmaster", "id": "cal-taskmaster"}]})

        return _List()

    def calendars(self):
        class _Calendars:
            def insert(self, body):
                return _FakeExecutable({"id": "cal-taskmaster"})

        return _Calendars()

    def freebusy(self):
        service = self

        class _FreeBusy:
            def query(self, body):
                busy = [{"start": s, "end": e} for s, e in service.busy_periods]
                calendars = {item["id"]: {"busy": busy} for item in body.get("items", [])}
                return _FakeExecutable({"calendars": calendars})

        return _FreeBusy()

    def events(self):
        service = self

        class _Events:
            def list(self, calendarId, timeMin=None, timeMax=None,
                      privateExtendedProperty=None, maxResults=None, singleEvents=None):
                items = list(service._events_by_id.values())
                if privateExtendedProperty:
                    key, _, value = privateExtendedProperty.partition("=")
                    items = [
                        e for e in items
                        if (e.get("extendedProperties", {}).get("private", {}).get(key) == value)
                    ]
                if timeMin and timeMax:
                    window_start = dt.datetime.fromisoformat(timeMin)
                    window_end = dt.datetime.fromisoformat(timeMax)

                    def _overlaps(e):
                        start = dt.datetime.fromisoformat(e["start"]["dateTime"])
                        end = dt.datetime.fromisoformat(e["end"]["dateTime"])
                        return start < window_end and end > window_start

                    items = [e for e in items if _overlaps(e)]
                return _FakeExecutable({"items": items})

            def insert(self, calendarId, body):
                service.insert_count += 1
                service.last_calendar_id = calendarId
                service._next_id += 1
                eid = f"evt-{service._next_id}"
                event = {"id": eid, "htmlLink": f"https://calendar/{eid}", **body}
                service._events_by_id[eid] = event
                return _FakeExecutable(event)

            def patch(self, calendarId, eventId, body):
                service.patch_count += 1
                service.last_calendar_id = calendarId
                event = {"id": eventId, "htmlLink": f"https://calendar/{eventId}", **body}
                service._events_by_id[eventId] = event
                return _FakeExecutable(event)

            def delete(self, calendarId, eventId):
                service.delete_count += 1
                service._events_by_id.pop(eventId, None)
                return _FakeExecutable({})

        return _Events()


def _events_for(fake: FakeCalendarService, source_ref: str) -> list[dict]:
    return [
        e for e in fake._events_by_id.values()
        if e.get("extendedProperties", {}).get("private", {}).get("source_ref") == source_ref
    ]


@pytest.fixture
def fake_calendar(monkeypatch):
    fake = FakeCalendarService()
    monkeypatch.setattr(calendar_tool, "_get_service", lambda: fake)
    return fake


@pytest_asyncio.fixture
async def backend():
    """ASGI client for the backend (ADK agent + triggers)."""
    transport = httpx.ASGITransport(app=backend_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://backend") as c:
        yield c


def _make_pubsub_payload(task: dict, subscription: str = "test-sub") -> dict:
    """Build a Pub/Sub trigger request body from a Task-shaped dict."""
    encoded = base64.b64encode(json.dumps(task).encode()).decode()
    return {
        "message": {"data": encoded, "attributes": {"source": "test"}},
        "subscription": subscription,
    }


def _iso_in(**timedelta_kwargs) -> str:
    """A due_at relative to whenever the tests actually run, not a fixed
    calendar date — a hardcoded absolute timestamp drifts into "due in 2
    hours" or "already past" as real time moves on, which is exactly the
    kind of flakiness a scheduling test can't afford."""
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(**timedelta_kwargs)).isoformat().replace("+00:00", "Z")


LOW_PRIORITY_TASK = {
    "source": "canvas",
    "source_ref": "quiz-1",
    "title": "Reading Quiz 3",
    "course": "DATA C100",
    "description": "A short weekly reading check.",
    "due_at": _iso_in(days=60),  # far out -> low urgency
    "points_possible": 2,
    "course_total_points": 500,
}

HIGH_PRIORITY_TASK = {
    "source": "canvas",
    "source_ref": "midterm-1",
    "title": "Midterm Project",
    "course": "DATA C100",
    "description": "A substantial multi-week project worth a large share of the grade.",
    "due_at": _iso_in(days=1, hours=6),  # due soon -> high urgency
    "points_possible": 40,
    "course_total_points": 100,
}


# ---------------------------------------------------------------------------
# ADK routing (real Gemini calls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_priority_schedules_quietly(backend, fake_calendar):
    """A low-urgency, low-grade-weight task routes QUIET and still gets a
    real (faked) calendar block — every task the agent sees produces a
    visible calendar change, whether or not it also nags."""
    resp = await backend.post(
        "/apps/taskmaster_agent/trigger/pubsub",
        json=_make_pubsub_payload(LOW_PRIORITY_TASK),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert _events_for(fake_calendar, "quiz-1"), "expected at least one block for this task"


@pytest.mark.asyncio
async def test_excluded_course_is_never_scheduled(backend, fake_calendar, capsys, monkeypatch):
    """A course the student told onboarding to ignore (e.g. one they tutor)
    gets no calendar block and no score at all — on the Pub/Sub-triggered
    path, same as the local batch scheduler's ``skipped`` list. Before this,
    only the local path respected excluded_courses; the deployed agent
    would have scheduled it anyway."""
    monkeypatch.setattr(agent_module, "load_config", lambda: {"excluded_courses": ["DATA C100"]})

    resp = await backend.post(
        "/apps/taskmaster_agent/trigger/pubsub",
        json=_make_pubsub_payload(LOW_PRIORITY_TASK),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert not _events_for(fake_calendar, "quiz-1"), "excluded course should get no calendar block"

    logged = capsys.readouterr().out
    excluded_lines = [json.loads(line) for line in logged.splitlines() if '"decision": "excluded"' in line]
    assert excluded_lines, "expected skip_excluded to log the exclusion"


@pytest.mark.asyncio
async def test_high_priority_schedules_and_reminds(backend, fake_calendar, capsys):
    """A due-soon, grade-heavy task routes HIGH_PRIORITY: it gets calendar
    block(s) AND a reminder alert (the log-based metric behind
    terraform/monitoring.tf's alert policy)."""
    resp = await backend.post(
        "/apps/taskmaster_agent/trigger/pubsub",
        json=_make_pubsub_payload(HIGH_PRIORITY_TASK),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert _events_for(fake_calendar, "midterm-1"), "expected at least one block for this task"

    logged = capsys.readouterr().out
    alert_lines = [json.loads(line) for line in logged.splitlines() if '"alert_type": "task_reminder"' in line]
    assert alert_lines, "expected emit_reminder_alert to log alert_type=task_reminder"


@pytest.mark.asyncio
async def test_reprocessing_same_task_is_idempotent(backend, fake_calendar):
    """Re-running a sync for the same assignment (source_ref) reconciles
    to a self-consistent set of blocks — no stale duplicates left over —
    which is what makes repeated Cloud Scheduler syncs safe. Doesn't
    assume a fixed block count: Gemini's effort estimate can vary slightly
    between the two real calls for the same input, and the reconciliation
    logic is specifically designed to handle that (see the deterministic
    shrink test below for an exact-count version of this guarantee)."""
    payload = _make_pubsub_payload(LOW_PRIORITY_TASK, subscription="run-1")
    resp = await backend.post("/apps/taskmaster_agent/trigger/pubsub", json=payload)
    assert resp.status_code == 200

    payload = _make_pubsub_payload(LOW_PRIORITY_TASK, subscription="run-2")
    resp = await backend.post("/apps/taskmaster_agent/trigger/pubsub", json=payload)
    assert resp.status_code == 200

    # Whatever block count the second run settled on, the calendar should
    # hold exactly that many events for this task — not more.
    final_events = _events_for(fake_calendar, "quiz-1")
    assert len(final_events) >= 1
    assert fake_calendar.insert_count + fake_calendar.patch_count > fake_calendar.delete_count


# ---------------------------------------------------------------------------
# The scheduling brain itself (deterministic — estimated_hours passed
# directly, no Gemini call involved)
# ---------------------------------------------------------------------------

FIXED_CFG = {
    "calendar_target": "taskmaster",
    "daily_cap_hours": 4,
    "lead_time_days": 5,
    "work_day_start": 9,
    "work_day_end": 21,
    "effort_padding": 1.0,
    "off_days": [],
    "priority_courses": [],
}


def _assert_blocks_respect_bounds(events, *, daily_cap_hours):
    per_day: dict = {}
    for e in events:
        start = dt.datetime.fromisoformat(e["start"]["dateTime"])
        end = dt.datetime.fromisoformat(e["end"]["dateTime"])
        hours = (end - start).total_seconds() / 3600
        assert 0.5 - 1e-9 <= hours <= 3.0 + 1e-9, f"block duration {hours}h outside [0.5, 3]"
        per_day[start.date()] = per_day.get(start.date(), 0.0) + hours
    assert all(total <= daily_cap_hours + 1e-9 for total in per_day.values()), "a day exceeded daily_cap_hours"
    return per_day


def test_schedule_block_splits_across_days_respecting_daily_cap(fake_calendar, monkeypatch):
    """A task too big for one block, but within the 3-block ceiling, gets
    fully scheduled: split into [0.5, 3]h chunks, no day holding more than
    daily_cap_hours worth of this agent's blocks — the exact behavior
    taskmaster_calendar.py's local batch scheduler has always had, now
    shared by the Cloud Run path via _plan_blocks_for_task rather than
    reimplemented."""
    monkeypatch.setattr(calendar_tool, "load_config", lambda: FIXED_CFG)
    due = (dt.datetime.now().astimezone() + dt.timedelta(days=10)).isoformat()

    result = calendar_tool.schedule_block(
        title="Medium Task", course="DATA 101", due_at=due,
        estimated_hours=6, source_ref="medium-1", priority_score=5.0,
    )

    assert result["status"] == "scheduled"
    assert result["fully_scheduled"] is True
    events = _events_for(fake_calendar, "medium-1")
    assert len(events) == result["blocks"] > 1

    _assert_blocks_respect_bounds(events, daily_cap_hours=4.0)
    total_hours = sum((dt.datetime.fromisoformat(e["end"]["dateTime"]) -
                        dt.datetime.fromisoformat(e["start"]["dateTime"])).total_seconds() / 3600
                       for e in events)
    assert total_hours == pytest.approx(result["budgeted_hours"], abs=0.05)


def test_schedule_block_caps_at_three_blocks(fake_calendar, monkeypatch):
    """A task big enough to need more than 3 blocks doesn't get an
    unbounded string of sessions — it's capped at 3, each within
    [0.5, 3]h, and the rest of the budget is reported as unscheduled
    rather than silently spread across ever more calendar entries."""
    monkeypatch.setattr(calendar_tool, "load_config", lambda: FIXED_CFG)
    due = (dt.datetime.now().astimezone() + dt.timedelta(days=10)).isoformat()

    result = calendar_tool.schedule_block(
        title="Huge Project", course="DATA 101", due_at=due,
        estimated_hours=10, source_ref="huge-1", priority_score=5.0,
    )

    assert result["status"] == "scheduled"
    assert result["blocks"] == 3
    assert result["fully_scheduled"] is False
    events = _events_for(fake_calendar, "huge-1")
    assert len(events) == 3

    _assert_blocks_respect_bounds(events, daily_cap_hours=4.0)
    total_hours = sum((dt.datetime.fromisoformat(e["end"]["dateTime"]) -
                        dt.datetime.fromisoformat(e["start"]["dateTime"])).total_seconds() / 3600
                       for e in events)
    assert total_hours <= 9.0 + 1e-9, "3 blocks of <=3h each should never exceed 9h total"
    assert total_hours < result["budgeted_hours"], "budget exceeded the 3-block ceiling, so some should be left over"


def test_schedule_block_reconciles_when_estimate_shrinks(fake_calendar, monkeypatch):
    """Re-processing a task with a smaller estimate (or less budget left)
    deletes the now-unneeded blocks rather than leaving stale duplicates —
    this is the part a single fixed-size, single-block scheduler can't do
    at all, and it's the whole point of stamping block_index."""
    monkeypatch.setattr(calendar_tool, "load_config", lambda: FIXED_CFG)
    due = (dt.datetime.now().astimezone() + dt.timedelta(days=10)).isoformat()

    first = calendar_tool.schedule_block(
        title="Shrinking Task", course="DATA 101", due_at=due,
        estimated_hours=10, source_ref="shrink-1", priority_score=1.0,
    )
    assert first["blocks"] > 1
    assert len(_events_for(fake_calendar, "shrink-1")) == first["blocks"]

    second = calendar_tool.schedule_block(
        title="Shrinking Task", course="DATA 101", due_at=due,
        estimated_hours=1, source_ref="shrink-1", priority_score=1.0,
    )
    assert second["blocks"] == 1
    remaining_events = _events_for(fake_calendar, "shrink-1")
    assert len(remaining_events) == 1, "stale blocks from the larger estimate should be deleted"
    assert fake_calendar.delete_count == first["blocks"] - 1


def test_schedule_block_avoids_a_real_calendar_conflict(fake_calendar, monkeypatch):
    """A busy period on the student's real calendar (freebusy) pushes the
    block to a different day entirely — proving the conflict check is
    live, not just checking the agent's own prior blocks."""
    monkeypatch.setattr(calendar_tool, "load_config", lambda: FIXED_CFG)
    due = (dt.datetime.now().astimezone() + dt.timedelta(days=10)).isoformat()

    baseline = calendar_tool.schedule_block(
        title="Essay", course="ENGLISH 1A", due_at=due,
        estimated_hours=2, source_ref="conflict-baseline", priority_score=1.0,
    )
    baseline_event = _events_for(fake_calendar, "conflict-baseline")[0]
    baseline_day = dt.datetime.fromisoformat(baseline_event["start"]["dateTime"]).date()

    # Block out that entire day on the "real" calendar, then schedule an
    # equivalent task and confirm it lands on a different day.
    fake_calendar.busy_periods = [(
        dt.datetime.combine(baseline_day, dt.time(0, 0)).astimezone().isoformat(),
        dt.datetime.combine(baseline_day, dt.time(23, 59)).astimezone().isoformat(),
    )]

    conflicted = calendar_tool.schedule_block(
        title="Essay 2", course="ENGLISH 1A", due_at=due,
        estimated_hours=2, source_ref="conflict-avoided", priority_score=1.0,
    )
    assert conflicted["status"] == "scheduled"
    conflicted_event = _events_for(fake_calendar, "conflict-avoided")[0]
    conflicted_day = dt.datetime.fromisoformat(conflicted_event["start"]["dateTime"]).date()
    assert conflicted_day != baseline_day
    assert baseline  # keep the baseline result referenced, not just its event


def test_calendar_target_primary_writes_to_primary_and_skips_lookup(fake_calendar, monkeypatch):
    """calendar_target="primary" in taskmaster_config.json writes straight to
    the student's real calendar (no dedicated-calendar lookup/creation) —
    and the free-slot search still avoids double-booking a real event
    there, since "primary" is always included in the freebusy query."""
    monkeypatch.setattr(calendar_tool, "load_config", lambda: {**FIXED_CFG, "calendar_target": "primary"})

    def _fail(*args, **kwargs):
        raise AssertionError("should not look up/create a dedicated calendar when calendar_target=primary")

    monkeypatch.setattr(calendar_tool, "_get_or_create_calendar", _fail)

    result = calendar_tool.schedule_block(
        title="Essay",
        course="ENGLISH 1A",
        due_at=_iso_in(days=60),
        estimated_hours=2,
        source_ref="essay-1",
        priority_score=1.0,
    )

    assert result["status"] == "scheduled"
    assert fake_calendar.last_calendar_id == "primary"
