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

from .config import config
from .models import Task
from .scoring import score_task, is_high_priority
from .prompts import EFFORT_ESTIMATOR_INSTRUCTION


# ---------------------------------------------------------------------------
# Function nodes
# ---------------------------------------------------------------------------


def parse_task_event(node_input: str) -> Event:
    """Parse a Pub/Sub trigger event and extract task data.

    The Canvas poller publishes a Task JSON in the ``data`` field, which may
    be base64-encoded (real Pub/Sub) or plain JSON (local testing).
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

    return Event(
        output={
            "source": data.get("source", "canvas"),
            "source_ref": str(data.get("source_ref", "")),
            "title": data.get("title", "Untitled task"),
            "course": data.get("course"),
            "description": data.get("description") or "",
            "due_at": data.get("due_at", ""),
            "points_possible": data.get("points_possible"),
            "course_total_points": data.get("course_total_points"),
        }
    )


def estimate_and_score(node_input: dict, ctx: Context) -> Event:
    """Estimate effort (via the LLM sub-agent's output already in state),
    compute the deterministic priority score, and route.

    The effort estimate is produced by ``effort_agent`` upstream and stored
    on the task. Here we finalize the score and decide the path:
    ``HIGH_PRIORITY`` (schedule + remind) vs ``QUIET`` (schedule only).
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

    task.priority_score = score_task(task)
    ctx.state["task"] = json.loads(task.model_dump_json())

    if is_high_priority(task, threshold=config.review_threshold):
        return Event(route="HIGH_PRIORITY", output=ctx.state["task"])
    return Event(route="QUIET", output=ctx.state["task"])


def schedule_quietly(node_input: dict) -> Event:
    """Low-priority task: log that it was scheduled, no nag."""
    log_entry = {
        "severity": "INFO",
        "message": (
            f"Task scheduled quietly: {node_input.get('title')} "
            f"(score {node_input.get('priority_score')})"
        ),
        "decision": "scheduled",
        "title": node_input.get("title"),
        "course": node_input.get("course"),
        "priority_score": node_input.get("priority_score"),
    }
    print(json.dumps(log_entry), flush=True)
    return Event(output={"status": "scheduled", **node_input})


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
    """Take the effort_agent's JSON output and store it on state for scoring."""
    est_hours = 2.0
    confidence = "low"
    try:
        text = node_input if isinstance(node_input, str) else json.dumps(node_input)
        # strip accidental markdown fences
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        parsed = json.loads(text)
        est_hours = float(parsed.get("estimated_hours", 2.0))
        confidence = parsed.get("confidence", "low")
    except Exception:
        pass
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
                "HIGH_PRIORITY": reminder_agent,
            },
        ),
    ],
)
