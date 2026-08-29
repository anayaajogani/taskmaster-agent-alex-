"""Voice agent backend.

Answers spoken questions about your coursework. The browser handles speech
(recognition + synthesis via the Web Speech API); this handles understanding.

Grounding matters here as much as anywhere: the agent answers ONLY from your
actual task data. If something isn't in the context, it says it doesn't know
rather than inventing an assignment. Same discipline as the syllabus reader.

Endpoints (served by run.py):
    POST /ask   {"question": "what should I do today?"}
         ->     {"answer": "...", "used_context": true}

Run standalone to test without speech:
    uv run python -m expense_agent.voice "what's due soonest?"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

_AGENT_ROOT = Path(__file__).resolve().parent.parent
DAILY_JSON = _AGENT_ROOT / "daily_view.json"
CONFIG_JSON = _AGENT_ROOT / "taskmaster_config.json"

GEMINI_MODEL = os.environ.get("VOICE_MODEL", "gemini-flash-latest")

SYSTEM_PROMPT = """\
You are the student's taskmaster assistant, answering out loud. You will be
given their real coursework data as JSON, then a spoken question.

RULES — these matter more than being helpful:
- Answer ONLY from the data provided. Never invent an assignment, deadline,
  reading, or course that isn't in the data.
- If the answer isn't in the data, say so plainly: "That's not in what I can
  see" — then say what you DO know that's closest.
- Never guess at grades, times you don't have, or what a professor wants.

STYLE — this is being spoken aloud, not read:
- Two or three sentences. Never lists, never markdown, never bullet points.
- Say dates like a person: "next Thursday", "the 24th", not "2026-09-24".
- Say durations naturally: "about half an hour", not "~25m est".
- Be direct and warm. No preamble like "Based on your data".
- If they have nothing urgent, say so honestly rather than manufacturing urgency.
"""


def load_context() -> dict:
    """Everything the agent is allowed to know, and nothing else."""
    ctx: dict = {}
    try:
        ctx["today"] = json.loads(DAILY_JSON.read_text())
    except Exception:
        ctx["today"] = None
    try:
        cfg = json.loads(CONFIG_JSON.read_text())
        # Only the parts relevant to answering questions.
        ctx["preferences"] = {
            "prioritizes_by": cfg.get("priority_mode"),
            "starts_days_before_deadline": cfg.get("lead_time_days"),
            "max_hours_per_day": cfg.get("daily_cap_hours"),
            "work_hours": f"{cfg.get('work_day_start')}:00-{cfg.get('work_day_end')}:00",
            "days_off": cfg.get("off_days", []),
            "priority_courses": cfg.get("priority_courses", []),
            "ignored_courses": cfg.get("excluded_courses", []),
        }
    except Exception:
        ctx["preferences"] = None
    return ctx


def _trim_context(ctx: dict) -> dict:
    """Keep the payload small — voice answers don't need every field."""
    today = ctx.get("today") or {}

    def slim_task(t):
        return {
            "title": t.get("title"),
            "course": (t.get("course") or "")[:40],
            "due": t.get("due"),
            "days_left": t.get("days_left"),
            "hours": t.get("hours"),
            "starts_in_days": t.get("opens_in_days"),
        }

    plan = today.get("study_plan") or {}
    return {
        "date": today.get("date"),
        "daily_cap_hours": today.get("daily_cap_hours"),
        "active_now": [slim_task(t) for t in today.get("active", [])],
        "not_started_yet": [slim_task(t) for t in today.get("upcoming", [])[:6]],
        "open_materials": [
            {"title": m.get("title"), "type": m.get("label"),
             "course": (m.get("course") or "")[:40]}
            for m in today.get("materials", [])
        ],
        "study_plan_today": {
            "free_minutes": plan.get("free_minutes"),
            "picks": [
                {"title": p.get("title"), "type": p.get("label"),
                 "est_minutes": p.get("est_minutes")}
                for p in plan.get("picks", [])
            ],
        } if plan else None,
        "preferences": ctx.get("preferences"),
    }


def ask(question: str) -> dict:
    """Answer a spoken question from the student's real data."""
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return {"answer": "I'm not connected to Gemini right now.",
                "used_context": False}

    ctx = _trim_context(load_context())
    has_data = bool(ctx.get("date"))

    payload = {
        "contents": [{
            "parts": [{
                "text": (
                    SYSTEM_PROMPT
                    + "\n\nSTUDENT'S DATA:\n"
                    + json.dumps(ctx, indent=2, default=str)
                    + "\n\nSPOKEN QUESTION: " + question
                    + "\n\nAnswer out loud in two or three sentences:"
                )
            }]
        }],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 400,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={api_key}")
    try:
        r = requests.post(url, json=payload, timeout=45)
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"].get("parts", [])
        text = " ".join(p.get("text", "") for p in parts if p.get("text")).strip()
        # Strip anything that reads badly aloud.
        text = text.replace("*", "").replace("#", "").replace("`", "")
        return {"answer": text, "used_context": has_data}
    except Exception as e:
        return {"answer": f"I couldn't work that out just now. {str(e)[:60]}",
                "used_context": has_data}


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "what should I work on today?"
    print(f"\n  Q: {q}")
    result = ask(q)
    print(f"  A: {result['answer']}\n")
