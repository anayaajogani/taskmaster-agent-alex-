

from __future__ import annotations

import base64
import json
import os
import time

import requests

from .canvas_poller import assignments_to_tasks

AGENT_URL = os.environ.get(
    "AGENT_URL",
    "http://localhost:8080/apps/expense_agent/trigger/pubsub",
)


def _wrap(task_json: str) -> dict:
    """Wrap a Task JSON string in the Pub/Sub message shape the agent expects."""
    data_b64 = base64.b64encode(task_json.encode("utf-8")).decode("utf-8")
    return {
        "message": {"data": data_b64, "attributes": {"source": "canvas"}},
        "subscription": "canvas-feed",
    }


def main() -> None:
    tasks = assignments_to_tasks()
    if not tasks:
        print("No upcoming assignments found.")
        return

    print(f"Feeding {len(tasks)} assignment(s) into the agent...\n")
    for t in tasks:
        payload = _wrap(t.model_dump_json())
        try:
            resp = requests.post(AGENT_URL, json=payload, timeout=60)
            status = resp.status_code
            body = resp.text[:120]
            print(f"  [{status}] {t.title[:50]}  ->  {body}")
        except Exception as e:
            print(f"  [ERR] {t.title[:50]}  ->  {e}")
        time.sleep(1)  # be gentle; the LLM call takes a moment each

    print("\nDone. Check the agent terminal for reminder/schedule logs.")


if __name__ == "__main__":
    main()
