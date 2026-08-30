"""Data models for the taskmaster agent.

This replaces the `ExpenseData` Pydantic model in the ambient-expense-agent
sample. A `Task` is the normalized unit that flows through the ADK graph:
it arrives (via the Canvas poller -> Pub/Sub), gets an LLM effort estimate,
gets a deterministic priority score, and is then routed (scheduled quietly
vs. scheduled + reminded).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Task(BaseModel):
    """A single actionable student task, normalized across sources."""

    source: str = Field(description="Origin of the task, e.g. 'canvas', 'gmail'.")
    source_ref: str = Field(
        description="Stable unique ID from the source. Used for dedupe so the "
        "same assignment isn't scheduled twice."
    )
    title: str = Field(description="Human-readable task title.")
    course: Optional[str] = Field(
        default=None, description="Course name or code, if known."
    )
    description: Optional[str] = Field(
        default=None,
        description="Assignment description text. Fed to the LLM for effort "
        "estimation. May be empty.",
    )
    due_at: datetime = Field(description="When the task is due (timezone-aware).")

    # Grade-impact inputs (from Canvas). Optional because not every source has them.
    points_possible: Optional[float] = Field(
        default=None, description="Points this assignment is worth."
    )
    course_total_points: Optional[float] = Field(
        default=None, description="Total points in the course, for normalizing impact."
    )

    # Filled in by the pipeline, not by the source.
    estimated_hours: Optional[float] = Field(
        default=None, description="LLM-estimated effort in hours."
    )
    estimate_confidence: Optional[str] = Field(
        default=None, description="LLM confidence: 'low' | 'medium' | 'high'."
    )
    priority_score: Optional[float] = Field(
        default=None, description="Deterministic 0-1+ score from score_task()."
    )

    def dedupe_key(self) -> str:
        """Stable key so Canvas + Gmail copies of one assignment collapse to one."""
        return f"{self.source}:{self.source_ref}"
