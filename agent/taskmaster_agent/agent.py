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

"""Ambient taskmaster agent that processes student assignments.

Adapted from the ambient-expense-agent sample. Assignments arrive via ADK
trigger endpoints (Pub/Sub, fed by the Canvas poller) and route through a
graph-based workflow:

- Low-priority tasks are scheduled quietly (the "auto-approve" path).
- High-priority tasks get an LLM effort estimate, are scored, and trigger
  an escalating reminder alert.

Business rules (the priority score, the threshold) live in code
(scoring.py); the LLM only estimates effort (prompts.py). This keeps the
ranking transparent and un-hallucinatable.
"""

import base64
import json
from datetime import datetime, timezone

from google.adk import Agent, Context, Event, Workflow

from .calendar_tool import schedule_block
from .config import config
from .models import Task
from .onboarding import load_config
from .scoring import score_task, is_high_priority
from .prompts import EFFORT_ESTIMATOR_INSTRUCTION
from .taskmaster_calendar import _is_excluded


# ---------------------------------------------------------------------------
# Function nodes
# ---------------------------------------------------------------------------


def parse_task_event(node_input: str, ctx: Context) -> Event:
    """Parse a Pub/Sub trigger event and extract task data.

    The Canvas poller publishes a Task JSON in the ``data`` field, which may
    be base64-encoded (real Pub/Sub) or plain JSON (local testing).

    Stores the parsed task on ``ctx.state["parsed_task"]`` — this is the only
    place it's written, and ``apply_effort_estimate`` is the only place that
    reads it back to merge the LLM's estimate on top. Without this the effort
    estimate has nothing to attach to and the task downstream is empty.
    """
    try:
        event = json.loads(node_input)
    except json.JSONDecodeError:
        return Event(output={"error": f"Invalid JSON: {node_input[:200]}"})

    data = event.get("data", {})

    if isinstance(data, str):
        try:
            data = json.loads(base64.b64decode(data))
        except Exception:
            return Event(output={"error": f"Failed to decode data: {data[:200]}"})

    parsed = {
        "source": data.get("source", "canvas"),
        "source_ref": str(data.get("source_ref", "")),
        "title": data.get("title", "Untitled task"),
        "course": data.get("course"),
        "description": data.get("description") or "",
        "due_at": data.get("due_at", ""),
        "points_possible": data.get("points_possible"),
        "course_total_points": data.get("course_total_points"),
    }
    ctx.state["parsed_task"] = parsed
    return Event(output=parsed)


def estimate_and_score(node_input: dict, ctx: Context) -> Event:
    """Estimate effort (via the LLM sub-agent's output already in state),
    compute the deterministic priority score, and route.

    The effort estimate is produced by ``effort_agent`` upstream and stored
    on the task. Here we finalize the score and decide the path:
    ``HIGH_PRIORITY`` (schedule + remind), ``QUIET`` (schedule only), or
    ``EXCLUDED`` (the student told onboarding to ignore this course —
    e.g. one they tutor — so it's not scheduled or scored at all).
    """
    # Build a Task from the parsed fields.
    try:
        due_raw = node_input.get("due_at", "")
        due_at = (
            datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
            if due_raw
            else datetime.now(timezone.utc)
        )
    except Exception:
        due_at = datetime.now(timezone.utc)

    task = Task(
        source=node_input.get("source", "canvas"),
        source_ref=node_input.get("source_ref", ""),
        title=node_input.get("title", "Untitled task"),
        course=node_input.get("course"),
        description=node_input.get("description") or None,
        due_at=due_at,
        points_possible=node_input.get("points_possible"),
        course_total_points=node_input.get("course_total_points"),
        estimated_hours=ctx.state.get("estimated_hours"),
        estimate_confidence=ctx.state.get("estimate_confidence"),
    )

    cfg = load_config()
    ctx.state["task"] = json.loads(task.model_dump_json())
    if _is_excluded(task, cfg):
        return Event(route="EXCLUDED", output=ctx.state["task"])

    task.priority_score = score_task(task)
    ctx.state["task"] = json.loads(task.model_dump_json())

    if is_high_priority(task):  # scoring.PRIORITY_THRESHOLD is the single source of truth
        return Event(route="HIGH_PRIORITY", output=ctx.state["task"])
    return Event(route="QUIET", output=ctx.state["task"])


def skip_excluded(node_input: dict) -> Event:
    """A task from a course the student told onboarding to ignore (e.g.
    one they tutor or TA for, in a way Canvas's own enrollment role
    doesn't catch). No calendar write, no score, no reminder — matches
    ``taskmaster_calendar.rebuild_calendar_and_brief``'s ``skipped`` list
    for the local batch scheduler. Without this node, only that batch
    path respected exclusions; the Pub/Sub-triggered path would schedule
    the excluded course anyway.
    """
    log_entry = {
        "severity": "INFO",
        "message": f"Task excluded, not scheduled: {node_input.get('title')} ({node_input.get('course')})",
        "decision": "excluded",
        "title": node_input.get("title"),
        "course": node_input.get("course"),
    }
    print(json.dumps(log_entry), flush=True)
    return Event(output={"status": "excluded", **node_input})


def schedule_quietly(node_input: dict) -> Event:
    """Low-priority task: write the calendar block(s), log it, no nag."""
    result = schedule_block(
        title=node_input.get("title", "Untitled task"),
        course=node_input.get("course") or "",
        due_at=node_input.get("due_at", ""),
        estimated_hours=node_input.get("estimated_hours"),
        source_ref=node_input.get("source_ref", ""),
        priority_score=node_input.get("priority_score", 0),
        points_possible=node_input.get("points_possible"),
        course_total_points=node_input.get("course_total_points"),
    )
    log_entry = {
        "severity": "INFO",
        "message": (
            f"Task scheduled quietly: {node_input.get('title')} "
            f"(score {node_input.get('priority_score')}) -> {result.get('status')}, "
            f"{result.get('blocks')} block(s), fully_scheduled={result.get('fully_scheduled')}"
        ),
        "decision": "scheduled",
        "title": node_input.get("title"),
        "course": node_input.get("course"),
        "priority_score": node_input.get("priority_score"),
        "calendar": result,
    }
    print(json.dumps(log_entry), flush=True)
    return Event(output={"status": "scheduled", "calendar": result, **node_input})


def schedule_and_flag(node_input: dict) -> Event:
    """High-priority task: write the calendar block(s) deterministically,
    then hand off to reminder_agent for just the alert.

    Scheduling is a consequential action with an idempotency guarantee to
    uphold (see calendar_tool.py) — it belongs in code, not delegated to
    an LLM asked to remember to make two separate tool calls in the right
    order every time. An earlier version had reminder_agent call both
    `schedule_block` and `emit_reminder_alert` itself; in practice it
    sometimes only made one of the two calls, which is exactly the
    reliability problem this file's own docstring warns about — business
    rules stay in code, the LLM only judges.
    """
    result = schedule_block(
        title=node_input.get("title", "Untitled task"),
        course=node_input.get("course") or "",
        due_at=node_input.get("due_at", ""),
        estimated_hours=node_input.get("estimated_hours"),
        source_ref=node_input.get("source_ref", ""),
        priority_score=node_input.get("priority_score", 0),
        points_possible=node_input.get("points_possible"),
        course_total_points=node_input.get("course_total_points"),
    )
    log_entry = {
        "severity": "INFO",
        "message": (
            f"High-priority task scheduled: {node_input.get('title')} "
            f"-> {result.get('status')}, {result.get('blocks')} block(s), "
            f"fully_scheduled={result.get('fully_scheduled')}"
        ),
        "decision": "scheduled_high_priority",
        "title": node_input.get("title"),
        "course": node_input.get("course"),
        "priority_score": node_input.get("priority_score"),
        "calendar": result,
    }
    print(json.dumps(log_entry), flush=True)
    return Event(output={"calendar": result, **node_input})


def emit_reminder_alert(
    title: str,
    course: str,
    due_at: str,
    priority_score: float,
    estimated_hours: float,
) -> dict:
    """Emit a structured log that triggers an escalating reminder email.

    Cloud Run captures JSON stdout as structured logs in Cloud Logging. A
    log-based metric + alert policy fire the reminder email (same mechanism
    the sample used for expense alerts).

    Args:
        title: The assignment title.
        course: The course it belongs to.
        due_at: When it is due (ISO string).
        priority_score: The computed priority score.
        estimated_hours: LLM effort estimate in hours.

    Returns:
        Confirmation the alert was emitted.
    """
    log_entry = {
        "severity": "WARNING",
        "message": (
            f"Reminder: '{title}' for {course} is due {due_at} "
            f"— est. {estimated_hours}h, priority {priority_score}. Get started."
        ),
        "alert_type": "task_reminder",
        "title": title,
        "course": course,
        "due_at": due_at,
        "priority_score": priority_score,
        "estimated_hours": estimated_hours,
    }
    print(json.dumps(log_entry), flush=True)
    return {"status": "reminder_emitted", "title": title}


# ---------------------------------------------------------------------------
# LLM effort-estimator agent (runs for every task before scoring)
# ---------------------------------------------------------------------------

effort_agent = Agent(
    name="effort_agent",
    model=config.model,
    mode="single_turn",
    instruction=EFFORT_ESTIMATOR_INSTRUCTION,
)


def apply_effort_estimate(node_input, ctx: Context) -> Event:
    """Take the effort_agent's JSON output and store it on state for scoring.

    The LLM can hallucinate an absurd number here (0.01h, 400h), and that
    number goes straight into calendar scheduling. So we clamp it to a range
    that's plausible for a single student assignment, and record when we had
    to intervene so it isn't silent.
    """
    MIN_HOURS, MAX_HOURS, DEFAULT_HOURS = 0.25, 20.0, 2.0

    est_hours = DEFAULT_HOURS
    confidence = "low"
    clamped = False
    try:
        text = node_input if isinstance(node_input, str) else json.dumps(node_input)
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        parsed = json.loads(text)
        raw = float(parsed.get("estimated_hours", DEFAULT_HOURS))
        if raw != raw or raw <= 0:  # NaN or nonsense
            est_hours, clamped = DEFAULT_HOURS, True
        elif raw < MIN_HOURS:
            est_hours, clamped = MIN_HOURS, True
        elif raw > MAX_HOURS:
            est_hours, clamped = MAX_HOURS, True
        else:
            est_hours = raw
        confidence = parsed.get("confidence", "low")
        if confidence not in ("low", "medium", "high"):
            confidence = "low"
    except Exception:
        est_hours, confidence, clamped = DEFAULT_HOURS, "low", True

    if clamped:
        confidence = "low"  # we overrode the model; don't present it as certain
        print(json.dumps({
            "severity": "INFO",
            "message": "Effort estimate was out of range and was clamped.",
            "clamped_to": est_hours,
        }), flush=True)

    ctx.state["estimated_hours"] = est_hours
    ctx.state["estimate_confidence"] = confidence
    return Event(output=ctx.state.get("parsed_task", {}))


# ---------------------------------------------------------------------------
# High-priority reminder agent (emits the nag)
# ---------------------------------------------------------------------------

reminder_agent = Agent(
    name="reminder_agent",
    model=config.model,
    mode="single_turn",
    instruction="""You handle a high-priority student task that needs a reminder.
Call the `emit_reminder_alert` tool with the task's title, course, due_at,
priority_score, and estimated_hours from the input. Then return a one-line
confirmation. Do not add commentary.""",
    tools=[emit_reminder_alert],
)


# ---------------------------------------------------------------------------
# Graph-based workflow — the root agent
# ---------------------------------------------------------------------------

root_agent = Workflow(
    name="taskmaster",
    edges=[
        ("START", parse_task_event, effort_agent, apply_effort_estimate, estimate_and_score),
        (
            estimate_and_score,
            {
                "QUIET": schedule_quietly,
                "HIGH_PRIORITY": schedule_and_flag,
                "EXCLUDED": skip_excluded,
            },
        ),
        (schedule_and_flag, reminder_agent),
    ],
)
