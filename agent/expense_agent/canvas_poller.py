"""Canvas -> Pub/Sub bridge.

The ambient-expense sample is triggered by expenses arriving on a Pub/Sub
topic. Canvas doesn't push to Pub/Sub, so this poller is the bridge: it reads
your bCourses assignments, converts each to a Task, and publishes the NEW ones
to the same topic the agent listens on.

Run it two ways:
  - Locally / on a loop for dev (python -m taskmaster_agent.canvas_poller)
  - As a Cloud Run job triggered by Cloud Scheduler in production (this is the
    'runs in the background autonomously' story for the demo).

Env vars:
  CANVAS_BASE_URL   e.g. https://bcourses.berkeley.edu
  CANVAS_TOKEN      your personal access token (bCourses > Account > Settings)
  PUBSUB_TOPIC      full topic path, or leave unset to just print (dev mode)
  GOOGLE_CLOUD_PROJECT   needed only when publishing to Pub/Sub
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Iterable

import requests

from .models import Task


CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://bcourses.berkeley.edu")
CANVAS_TOKEN = os.environ.get("CANVAS_TOKEN", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {CANVAS_TOKEN}"}


def _get(path: str, params: dict | None = None) -> list | dict:
    """GET the Canvas REST API, following pagination."""
    url = f"{CANVAS_BASE_URL}/api/v1{path}"
    results: list = []
    while url:
        resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return data
        # Canvas paginates via a Link header with rel="next"
        url = resp.links.get("next", {}).get("url")
        params = None  # params only needed on the first request
    return results


def fetch_active_courses() -> list[dict]:
    """Courses the student is currently enrolled in."""
    return _get("/courses", params={"enrollment_state": "active", "per_page": 100})


def fetch_assignments(course_id: int) -> list[dict]:
    """All assignments for a course, including point values."""
    return _get(f"/courses/{course_id}/assignments", params={"per_page": 100})


def _parse_due(raw: str | None) -> datetime | None:
    if not raw:
        return None
    # Canvas returns ISO 8601 with a trailing Z
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def assignments_to_tasks(skip_teaching: bool = False) -> list[Task]:
    """Pull every course's assignments and normalize to Task objects.

    Skips assignments with no due date and ones already past due.

    Courses where Canvas says you're a TA/teacher are KEPT but tagged
    work_type="teaching" — grading and prep are real work, they're just a
    different kind than your own coursework. Pass skip_teaching=True to drop
    them entirely instead.

    Note the manual exclusion list in the config still removes courses
    outright; that's for cases Canvas gets wrong (e.g. tutors enrolled as
    students).
    """
    tasks: list[Task] = []
    teaching_courses: list[str] = []
    now = datetime.now(timezone.utc)

    for course in fetch_active_courses():
        course_id = course.get("id")
        course_name = course.get("name") or course.get("course_code") or str(course_id)

        teaches = is_teaching_role(course)
        if teaches:
            teaching_courses.append(f"{course_name} (you are {course_role(course)})")
            if skip_teaching:
                continue

        for a in fetch_assignments(course_id):
            due = _parse_due(a.get("due_at"))
            if due is None or due < now:
                continue  # no deadline or already passed -> skip

            tasks.append(
                Task(
                    source="canvas",
                    work_type="teaching" if teaches else "coursework",
                    source_ref=str(a.get("id")),
                    title=a.get("name", "Untitled assignment"),
                    course=course_name,
                    description=(a.get("description") or "")[:2000] or None,
                    due_at=due,
                    points_possible=a.get("points_possible"),
                    course_total_points=None,
                )
            )

    assignments_to_tasks.last_skipped_teaching = (  # type: ignore
        teaching_courses if skip_teaching else []
    )
    assignments_to_tasks.last_teaching_courses = teaching_courses  # type: ignore
    return tasks


def _publish(tasks: Iterable[Task]) -> None:
    """Publish each task to Pub/Sub, or print if no topic is configured (dev)."""
    topic = os.environ.get("PUBSUB_TOPIC")
    if not topic:
        for t in tasks:
            print(json.dumps(json.loads(t.model_dump_json()), indent=2, default=str))
        return

    # Lazy import so local dev doesn't need the Pub/Sub client installed.
    from google.cloud import pubsub_v1  # type: ignore

    publisher = pubsub_v1.PublisherClient()
    for t in tasks:
        payload = t.model_dump_json().encode("utf-8")
        # Match the sample's message shape: base64 data + attributes.
        publisher.publish(topic, data=payload, source="canvas")


def run_once() -> int:
    """One poll cycle. Returns count of tasks published."""
    if not CANVAS_TOKEN:
        raise SystemExit(
            "Set CANVAS_TOKEN (bCourses > Account > Settings > New Access Token)."
        )
    tasks = assignments_to_tasks()
    _publish(tasks)
    return len(tasks)


if __name__ == "__main__":
    count = run_once()
    print(f"\nProcessed {count} upcoming assignment(s).")


# ---------------------------------------------------------------------------
# Role detection
# ---------------------------------------------------------------------------

# Canvas enrollment types that mean "you teach/support this course" rather
# than "you take it". Assignments in these courses are other people's work.
TEACHING_ROLES = {"ta", "teacher", "designer", "TaEnrollment", "TeacherEnrollment"}


def course_role(course: dict) -> str:
    """Return the user's role in a course: 'student', 'ta', 'teacher', etc.

    Canvas reports this in the course's `enrollments` list. Note this is only
    as accurate as the enrollment itself — at some schools tutors and readers
    are enrolled as plain students, so this can't catch every case. The
    manual exclusion list in the config covers those.
    """
    roles = [e.get("type", "") for e in (course.get("enrollments") or [])]
    for r in roles:
        if r in TEACHING_ROLES or r.lower().replace("enrollment", "") in TEACHING_ROLES:
            return r.lower().replace("enrollment", "") or r
    return roles[0].lower().replace("enrollment", "") if roles else "unknown"


def is_teaching_role(course: dict) -> bool:
    """True if the user teaches/TAs this course rather than taking it."""
    return course_role(course) in TEACHING_ROLES
