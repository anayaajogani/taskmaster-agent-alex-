"""Terminal onboarding for the Taskmaster agent.

Asks the student a short set of questions and writes taskmaster_config.json.
The scheduler reads that config instead of using hardcoded defaults, so every
answer changes real behavior.

Run:
    uv run python -m taskmaster_agent.onboarding
"""

from __future__ import annotations

import json
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = _AGENT_ROOT / "taskmaster_config.json"


def _ask_choice(question: str, options: list[str]) -> int:
    """Ask a numbered multiple-choice question. Returns the chosen index."""
    print(f"\n{question}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}) {opt}")
    while True:
        raw = input("  > ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(f"  Please enter a number 1-{len(options)}.")


def _ask_text(question: str, default: str = "") -> str:
    print(f"\n{question}")
    if default:
        print(f"  (press Enter for: {default})")
    raw = input("  > ").strip()
    return raw or default


def run_onboarding() -> dict:
    print("=" * 60)
    print("  TASKMASTER SETUP")
    print("  A few questions so I schedule your work the way you want.")
    print("=" * 60)

    # Q1 - what dominates the priority score
    q1 = _ask_choice(
        "1) When everything's due at once, what should I prioritize first?",
        [
            "Whatever's worth more of my grade",
            "Whatever's due soonest",
            "Whatever takes longest",
            "Whatever I've been putting off",
        ],
    )
    priority_mode = ["grade", "urgency", "effort", "avoidance"][q1]

    # Q2 - lead time
    q2 = _ask_choice(
        "2) How far ahead of a deadline do you like to start big assignments?",
        ["The day of", "1 to 2 days", "3 to 5 days", "A week or more"],
    )
    lead_time_days = [0, 2, 5, 7][q2]

    # Q3 - reminder aggressiveness
    q3 = _ask_choice(
        "3) How aggressive should my reminders be?",
        [
            "One heads-up and done",
            "Gentle, ramping up as it gets close",
            "Persistent for high-stakes work",
            "Relentless until it's finished",
        ],
    )
    reminder_style = ["minimal", "ramping", "persistent", "relentless"][q3]

    # Q4 - quiet hours + off days
    quiet_start = _ask_text(
        "4a) What time should I stop scheduling each night? (24h, e.g. 21)", "21"
    )
    quiet_end = _ask_text(
        "4b) What time can I start scheduling each morning? (24h, e.g. 9)", "9"
    )
    off_days_raw = _ask_text(
        "4c) Any full days to keep clear? (e.g. Sat,Sun — or leave blank)", ""
    )
    off_days = [d.strip().title()[:3] for d in off_days_raw.split(",") if d.strip()]

    # Q5 - course priorities + exclusions
    print("\n5) Now your courses.")
    priority_courses_raw = _ask_text(
        "5a) Which courses matter most this term? (comma separated, partial names OK)", ""
    )
    priority_courses = [c.strip() for c in priority_courses_raw.split(",") if c.strip()]

    excluded_raw = _ask_text(
        "5b) Any courses I should IGNORE? (e.g. classes you tutor or TA, "
        "not classes you take — comma separated)",
        "",
    )
    excluded_courses = [c.strip() for c in excluded_raw.split(",") if c.strip()]

    # Non-Canvas courses
    non_canvas = _ask_text(
        "5c) Any courses that DON'T use Canvas? Give name + URL if so "
        "(I'll flag these for you to check manually)",
        "",
    )

    # Q6 - daily cap
    q6 = _ask_choice(
        "6) How many hours a day, at most, should I schedule you for coursework?",
        ["1 to 2", "3 to 4", "5 or more", "No limit"],
    )
    daily_cap_hours = [2, 4, 6, 24][q6]

    # Q7 - estimate accuracy -> effort padding
    q7 = _ask_choice(
        "7) How good are you at estimating how long work takes?",
        ["Usually accurate", "I tend to underestimate", "No idea"],
    )
    effort_padding = [1.0, 1.3, 1.2][q7]

    # Q8 - where the agent-triggered scheduler writes blocks. Only applies to
    # the automatic path (calendar_tool.py, Pub/Sub -> ADK -> Gemini): it
    # writes one block at a time and never deletes anything it didn't create
    # itself, so writing to your primary calendar is safe there. The local
    # dashboard scheduler (taskmaster_calendar.py) always uses its own
    # dedicated calendar regardless of this setting, because it wipes and
    # rebuilds its calendar's future events on every run — doing that against
    # your primary calendar would delete your real events.
    q8 = _ask_choice(
        "8) Where should the agent put automatically-scheduled work blocks?",
        ["A new dedicated calendar (safest)", "My primary calendar"],
    )
    calendar_target = ["taskmaster", "primary"][q8]

    config = {
        "priority_mode": priority_mode,
        "lead_time_days": lead_time_days,
        "reminder_style": reminder_style,
        "work_day_start": int(quiet_end or 9),
        "work_day_end": int(quiet_start or 21),
        "off_days": off_days,
        "priority_courses": priority_courses,
        "excluded_courses": excluded_courses,
        "non_canvas_courses": non_canvas,
        "daily_cap_hours": daily_cap_hours,
        "effort_padding": effort_padding,
        "calendar_target": calendar_target,
    }

    CONFIG_PATH.write_text(json.dumps(config, indent=2))

    # Read it back to the user so they can confirm the interpretation.
    print("\n" + "=" * 60)
    print("  HERE'S HOW I'LL WORK:")
    print("=" * 60)
    print(f"  Priority driver:   {priority_mode}")
    print(f"  Start work:        {lead_time_days} day(s) before deadlines")
    print(f"  Reminders:         {reminder_style}")
    print(f"  Scheduling window: {config['work_day_start']}:00 - {config['work_day_end']}:00")
    if off_days:
        print(f"  Days kept clear:   {', '.join(off_days)}")
    print(f"  Max per day:       {daily_cap_hours}h")
    if priority_courses:
        print(f"  Priority courses:  {', '.join(priority_courses)}")
    if excluded_courses:
        print(f"  Ignoring:          {', '.join(excluded_courses)}")
    if non_canvas:
        print(f"  Manual check:      {non_canvas}")
    target_label = "your primary calendar" if calendar_target == "primary" else "a new dedicated calendar"
    print(f"  Auto-scheduled blocks go to: {target_label}")
    print(f"\n  Saved to {CONFIG_PATH.name}")
    print("  Run the scheduler next: uv run python -m taskmaster_agent.taskmaster_calendar\n")

    return config


def load_config() -> dict:
    """Load saved config, or sensible defaults if onboarding hasn't run."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {
        "priority_mode": "grade",
        "lead_time_days": 5,
        "reminder_style": "ramping",
        "work_day_start": 9,
        "work_day_end": 21,
        "off_days": [],
        "priority_courses": [],
        "excluded_courses": [],
        "non_canvas_courses": "",
        "daily_cap_hours": 4,
        "effort_padding": 1.2,
        "calendar_target": "taskmaster",
    }


if __name__ == "__main__":
    run_onboarding()
