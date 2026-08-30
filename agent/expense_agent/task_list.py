"""Persistent task list output.

The terminal briefing scrolls away. This writes the ranked task list to two
files that update on every scheduler run:

  TASK_LIST.md    - human-readable, open it anytime to see what to work on
  task_list.json  - machine-readable, so the React frontend can render it

Both live in the agent/ root.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import os as _os
_AGENT_ROOT = Path(_os.environ.get('STATE_DIR')) \
    if _os.environ.get('STATE_DIR') \
    else Path(__file__).resolve().parent.parent
MD_PATH = _AGENT_ROOT / "TASK_LIST.md"
JSON_PATH = _AGENT_ROOT / "task_list.json"

_COLOR_LABEL = {
    "11": "critical",
    "6": "soon",
    "5": "upcoming",
    "10": "later",
    "3": "priority-course",
}


def _urgency_label(due_str: str, days_out: float) -> str:
    if days_out <= 2:
        return "DO NOW"
    if days_out <= 5:
        return "This week"
    if days_out <= 14:
        return "Coming up"
    return "Later"


def write_task_list(briefing: list[dict], skipped: list[str], cfg: dict) -> None:
    """Write the ranked list to Markdown + JSON."""
    now = dt.datetime.now().astimezone()

    # ---- JSON (for the frontend) ----
    payload = {
        "generated_at": now.isoformat(),
        "config": {
            "priority_mode": cfg.get("priority_mode"),
            "daily_cap_hours": cfg.get("daily_cap_hours"),
            "work_window": f"{cfg.get('work_day_start')}:00-{cfg.get('work_day_end')}:00",
            "off_days": cfg.get("off_days", []),
        },
        "tasks": briefing,
        "ignored": skipped,
        "non_canvas_courses": cfg.get("non_canvas_courses", ""),
    }
    JSON_PATH.write_text(json.dumps(payload, indent=2, default=str))

    # ---- Markdown (for you) ----
    lines = []
    lines.append("# Task List")
    lines.append("")
    lines.append(f"_Updated {now:%a %b %d, %I:%M %p}_")
    lines.append("")
    lines.append(
        f"Prioritizing by **{cfg.get('priority_mode')}** · "
        f"max **{cfg.get('daily_cap_hours')}h/day** · "
        f"working **{cfg.get('work_day_start')}:00–{cfg.get('work_day_end')}:00**"
        + (f" · off: {', '.join(cfg['off_days'])}" if cfg.get("off_days") else "")
    )
    lines.append("")

    if not briefing:
        lines.append("Nothing upcoming. Enjoy it.")
    else:
        lines.append("| # | Task | Course | Due | Budgeted | Blocks | Status |")
        lines.append("|---|------|--------|-----|----------|--------|--------|")
        for i, b in enumerate(briefing, 1):
            star = " ⭐" if b.get("priority_course") else ""
            status = "scheduled" if b.get("fully_scheduled") else "**tight**"
            lines.append(
                f"| {i} | {b['title']}{star} | {b.get('course') or ''} | {b['due']} "
                f"| {b['budgeted_hours']}h | {b['blocks']} | {status} |"
            )

    tight = [b for b in briefing if not b.get("fully_scheduled")]
    if tight:
        lines.append("")
        lines.append("## Needs attention")
        lines.append("")
        lines.append("Not enough free time before the deadline for these:")
        lines.append("")
        for b in tight:
            lines.append(f"- **{b['title']}** ({b.get('course')}) — due {b['due']}")

    if skipped:
        lines.append("")
        lines.append("## Ignored")
        lines.append("")
        for s in skipped:
            lines.append(f"- {s}")

    if cfg.get("non_canvas_courses"):
        lines.append("")
        lines.append("## Check manually (not on Canvas)")
        lines.append("")
        lines.append(f"- {cfg['non_canvas_courses']}")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "**Calendar colors:** red = due <2 days · orange = <5 days · "
        "yellow = <2 weeks · green = later · purple = priority course"
    )
    lines.append("")

    MD_PATH.write_text("\n".join(lines))
    print(f"  Task list written to {MD_PATH.name} and {JSON_PATH.name}")
