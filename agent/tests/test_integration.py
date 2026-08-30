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
(no LLM mocking — same assumption the original ambient-expense-agent test
suite made), so these need real credentials configured (GOOGLE_API_KEY or
Vertex AI application-default credentials), same as `make dev`.

What's mocked: Google Calendar. ``calendar_tool.schedule_block`` looks up
``_get_service`` by name in its own module at call time, so patching
``calendar_tool._get_service`` intercepts the real Calendar write no matter
which route calls it — the QUIET path calls ``schedule_block`` directly,
the HIGH_PRIORITY path calls it as a Gemini function-calling tool on
``reminder_agent``. One fake, both paths covered.
"""

import base64
import json

import httpx
import pytest
import pytest_asyncio

from expense_agent import calendar_tool
from expense_agent.fast_api_app import app as backend_app


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeCalendarService:
    """Minimal stand-in for the googleapiclient Calendar resource.

    Tracks inserts/patches by ``source_ref`` so tests can assert on
    idempotency (a second call with the same source_ref patches, not
    duplicates) without touching a real calendar.
    """

    def __init__(self):
        self.events_by_ref: dict[str, dict] = {}
        self.insert_count = 0
        self.patch_count = 0
        self.last_calendar_id: str | None = None

    def calendarList(self):  # noqa: N802 - matches googleapiclient's naming
        service = self

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
        cal_id = "cal-taskmaster"

        class _FreeBusy:
            def query(self, body):
                return _FakeExecutable({"calendars": {cal_id: {"busy": []}}})

        return _FreeBusy()

    def events(self):
        service = self

        class _Events:
            def list(self, calendarId, privateExtendedProperty=None, maxResults=1):
                ref = (privateExtendedProperty or "").removeprefix("source_ref=")
                existing = service.events_by_ref.get(ref)
                return _FakeExecutable({"items": [existing] if existing else []})

            def insert(self, calendarId, body):
                service.insert_count += 1
                service.last_calendar_id = calendarId
                ref = body["extendedProperties"]["private"]["source_ref"]
                event = {"id": f"evt-{ref}", "htmlLink": f"https://calendar/evt-{ref}"}
                service.events_by_ref[ref] = event
                return _FakeExecutable(event)

            def patch(self, calendarId, eventId, body):
                service.patch_count += 1
                return _FakeExecutable({"id": eventId, "htmlLink": f"https://calendar/{eventId}"})

        return _Events()


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


LOW_PRIORITY_TASK = {
    "source": "canvas",
    "source_ref": "quiz-1",
    "title": "Reading Quiz 3",
    "course": "DATA C100",
    "description": "A short weekly reading check.",
    "due_at": "2026-12-01T04:00:00Z",  # far out -> low urgency
    "points_possible": 2,
    "course_total_points": 500,
}

HIGH_PRIORITY_TASK = {
    "source": "canvas",
    "source_ref": "midterm-1",
    "title": "Midterm Project",
    "course": "DATA C100",
    "description": "A substantial multi-week project worth a large share of the grade.",
    "due_at": "2026-08-30T04:00:00Z",  # due tomorrow -> high urgency
    "points_possible": 40,
    "course_total_points": 100,
}


@pytest.mark.asyncio
async def test_low_priority_schedules_quietly(backend, fake_calendar):
    """A low-urgency, low-grade-weight task routes QUIET and still gets a
    real (faked) calendar block — every task the agent sees produces a
    visible calendar change, whether or not it also nags."""
    resp = await backend.post(
        "/apps/expense_agent/trigger/pubsub",
        json=_make_pubsub_payload(LOW_PRIORITY_TASK),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert fake_calendar.insert_count == 1
    assert "quiz-1" in fake_calendar.events_by_ref


@pytest.mark.asyncio
async def test_high_priority_schedules_and_reminds(backend, fake_calendar, capsys):
    """A due-soon, grade-heavy task routes HIGH_PRIORITY: it gets a calendar
    block AND a reminder alert (the log-based metric behind
    terraform/monitoring.tf's alert policy)."""
    resp = await backend.post(
        "/apps/expense_agent/trigger/pubsub",
        json=_make_pubsub_payload(HIGH_PRIORITY_TASK),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    assert fake_calendar.insert_count == 1
    assert "midterm-1" in fake_calendar.events_by_ref

    logged = capsys.readouterr().out
    alert_lines = [json.loads(line) for line in logged.splitlines() if '"alert_type": "task_reminder"' in line]
    assert alert_lines, "expected emit_reminder_alert to log alert_type=task_reminder"


@pytest.mark.asyncio
async def test_reprocessing_same_task_is_idempotent(backend, fake_calendar):
    """Re-running a sync for the same assignment (source_ref) patches the
    existing calendar block instead of creating a duplicate — this is what
    makes repeated Cloud Scheduler syncs safe."""
    payload = _make_pubsub_payload(LOW_PRIORITY_TASK, subscription="run-1")
    resp = await backend.post("/apps/expense_agent/trigger/pubsub", json=payload)
    assert resp.status_code == 200

    payload = _make_pubsub_payload(LOW_PRIORITY_TASK, subscription="run-2")
    resp = await backend.post("/apps/expense_agent/trigger/pubsub", json=payload)
    assert resp.status_code == 200

    assert fake_calendar.insert_count == 1
    assert fake_calendar.patch_count == 1


def test_calendar_target_primary_writes_to_primary_and_skips_lookup(fake_calendar, monkeypatch):
    """calendar_target="primary" in taskmaster_config.json writes straight to
    the student's real calendar (no dedicated-calendar lookup/creation) —
    and the free-slot search still avoids double-booking a real event
    there, since "primary" is always included in the freebusy query."""
    monkeypatch.setattr(calendar_tool, "load_config", lambda: {"calendar_target": "primary"})

    def _fail(*args, **kwargs):
        raise AssertionError("should not look up/create a dedicated calendar when calendar_target=primary")

    monkeypatch.setattr(calendar_tool, "_get_or_create_calendar", _fail)

    result = calendar_tool.schedule_block(
        title="Essay",
        course="ENGLISH 1A",
        due_at="2026-12-01T04:00:00Z",
        estimated_hours=2,
        source_ref="essay-1",
        priority_score=1.0,
    )

    assert result["status"] == "scheduled"
    assert fake_calendar.last_calendar_id == "primary"
