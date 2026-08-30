"""Deterministic prioritization.

This is the 'business rules stay in code' half of the ambient-agent pattern.
The LLM never decides priority order - it only estimates effort (see prompts.py).
Keeping the ranking here means it's transparent, debuggable, and can't be
hallucinated. The student's survey answers tune the WEIGHTS; the math is fixed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Task


# These defaults get overridden per-student by the onboarding survey.
# See UserProfile mapping - each survey answer nudges one of these.
DEFAULT_WEIGHTS = {
    "grade_impact": 0.5,   # how much an assignment's grade weight matters
    "effort": 0.1,         # how much estimated size matters
    "urgency_floor_hours": 1.0,  # avoid divide-by-zero on things due now
}

# If a task's score is at/above this, it's "high priority": it gets work blocks
# scheduled AND escalating reminders. Below it, it's quietly scheduled only.
# This is the ambient sample's `review_threshold`, repurposed.
PRIORITY_THRESHOLD = 0.6


def score_task(task: Task, weights: dict | None = None) -> float:
    """Return a priority score. Higher = more urgent/important.

    Transparent formula:
        urgency      = 1 / hours_until_due   (sooner -> higher)
        grade_impact = points / course_total (bigger share of grade -> higher)
        effort_bump  = larger tasks nudged up so they get started earlier

    All three combine multiplicatively so a task that's soon AND heavy AND
    grade-critical rises to the top, while a trivial far-off task stays low.
    """
    w = weights or DEFAULT_WEIGHTS

    now = datetime.now(timezone.utc)
    hours_left = (task.due_at - now).total_seconds() / 3600
    hours_left = max(hours_left, w["urgency_floor_hours"])
    # Scale so "due within ~24h" -> urgency near 1.0, decaying for later work.
    # 24 in the numerator makes the threshold interpretable on a 0-1 range.
    urgency = 24.0 / hours_left

    if task.points_possible and task.course_total_points:
        grade_impact = task.points_possible / task.course_total_points
    else:
        grade_impact = 0.3  # neutral default when Canvas doesn't give totals

    effort = task.estimated_hours or 2.0

    score = urgency * (1.0 + w["grade_impact"] * grade_impact) * (1.0 + w["effort"] * effort)
    return round(score, 4)


def is_high_priority(task: Task, threshold: float = PRIORITY_THRESHOLD) -> bool:
    """Routing decision for the ADK graph: schedule+remind vs. schedule quietly."""
    if task.priority_score is None:
        task.priority_score = score_task(task)
    return task.priority_score >= threshold
