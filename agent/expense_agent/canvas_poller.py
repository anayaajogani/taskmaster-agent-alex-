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
from dotenv import load_dotenv

from .models import Task

load_dotenv()  # read .env before the token is captured below


CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://bcourses.berkeley.edu")
CANVAS_TOKEN = os.environ.get("CANVAS_TOKEN", "")


def _token() -> str:
    """Read fresh each call so a rotated token only needs a restart."""
    return (os.environ.get("CANVAS_TOKEN") or CANVAS_TOKEN or "").strip()


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


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
    """Courses the student is currently enrolled in.

    Two Canvas quirks this works around:

    1. `enrollment_state=active` EXCLUDES courses the instructor hasn't
       published yet. Early in a term that can be half your schedule — the
       student sees the class on their dashboard, but the API pretends it
       doesn't exist. We ask for unpublished ones too and filter by state
       ourselves.
    2. Some courses don't carry the term in their name, so we request the
       `term` object rather than string-matching the title.
    """
    seen: dict = {}

    # published + active
    try:
        for c in _get("/courses", params={
            "enrollment_state": "active",
            "include[]": "term",
            "per_page": 100,
        }) or []:
            seen[c.get("id")] = c
    except Exception:
        pass

    # unpublished courses you're still enrolled in (invited/pending too)
    for state in ("invited_or_pending", "completed"):
        try:
            for c in _get("/courses", params={
                "enrollment_state": state,
                "include[]": "term",
                "per_page": 100,
            }) or []:
                seen.setdefault(c.get("id"), c)
        except Exception:
            pass

    # and the unfiltered list, which is what actually surfaces "unpublished"
    try:
        for c in _get("/courses", params={
            "include[]": "term",
            "per_page": 100,
        }) or []:
            state = (c.get("workflow_state") or "").lower()
            if state in ("available", "unpublished", "claimed"):
                seen.setdefault(c.get("id"), c)
    except Exception:
        pass

    return list(seen.values())


def course_term(course: dict) -> str:
    """The course's term name, e.g. 'Fall 2026'. Empty if Canvas didn't say."""
    return ((course.get("term") or {}).get("name") or "").strip()


def current_term_name() -> str:
    """Work out which term is running right now, from Canvas itself.

    Hardcoding "Fall 2026" would work for one student in one semester and
    silently break for everyone else. Instead: ask Canvas for the terms of the
    courses you're enrolled in, and pick the one whose date range covers today.
    Falls back to the most common term across active courses, then to "".
    """
    try:
        courses = fetch_active_courses()
    except Exception:
        return ""

    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}

    for c in courses:
        term = c.get("term") or {}
        name = (term.get("name") or "").strip()
        if not name or name.lower() in _USELESS_TERMS:
            continue
        counts[name] = counts.get(name, 0) + 1

        # if Canvas gives dates, trust them over any heuristic
        start, end = term.get("start_at"), term.get("end_at")
        try:
            if start and end:
                s_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                e_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                if s_dt <= now <= e_dt:
                    return name
        except Exception:
            pass

    if not counts:
        return ""

    # No dated term covered today — fall back to whichever term most of your
    # active courses belong to, which is almost always the current one.
    return max(counts.items(), key=lambda kv: kv[1])[0]


# Canvas at many schools reports "Default Term" for everything, so term-based
# filtering is unreliable. Rather than guess, we identify a real academic course
# by the things that actually distinguish one: a course code, an enrollment in
# a graded section, and NOT being a compliance/training module.

# Non-academic courses Canvas serves alongside real ones. These are mandatory
# trainings, orientations and admin shells — they have no grades and no
# deadlines that belong on a study schedule.
_NON_ACADEMIC_PATTERNS = (
    "student training", "shape ", "orientation", "golden bear",
    "enrollment course", "advising", "guide for", "onboarding",
    "compliance", "training -", "prep ",
)


def taking_list() -> list[str]:
    """Courses the student explicitly said they're taking, from onboarding."""
    try:
        from .onboarding import load_config
        return [t.strip().lower() for t in (load_config().get("taking_courses") or [])
                if t.strip()]
    except Exception:
        return []


def is_taking(course: dict) -> bool:
    """True if this is one of the courses the student named as theirs.

    Explicit beats inferred: after several rounds of trying to guess which of
    55 Canvas courses are the student's current classes, asking once is more
    reliable than any heuristic.
    """
    taking = taking_list()
    if not taking:
        return True  # nothing configured yet — don't hide anything
    name = (course.get("name") or "").lower()
    return any(t in name or name.startswith(t[:20]) for t in taking)


def is_academic_course(course: dict) -> bool:
    """True if this looks like a class the student takes or teaches.

    Deliberately conservative: a mandatory harassment-training module has
    dozens of 'pages' that would swamp a study plan, and it isn't coursework.
    """
    name = (course.get("name") or "").lower()
    if not name:
        return False
    return not any(p in name for p in _NON_ACADEMIC_PATTERNS)


_USELESS_TERMS = {"default term", "term", ""}


def in_term(course: dict, term: str) -> bool:
    """True if the course belongs to `term`.

    Degrades safely: when Canvas gives no usable term (the common case), keep
    the course. Hiding a class the student is enrolled in is a far worse
    failure than showing one extra.
    """
    _useless = {"default term", "term", ""}
    if not term or term.strip().lower() in _useless:
        return True

    t = course_term(course)
    if t and t.strip().lower() not in _useless:
        return term.lower() in t.lower()

    name = (course.get("name") or "").lower()
    if any(season in name for season in ("fall", "spring", "summer", "winter")):
        return term.lower() in name
    return True


def course_roster(current_term: str | None = None) -> list[dict]:
    """Every current course, INCLUDING ones with nothing posted yet.

    The task list only shows courses that have work. That's correct, but it
    leaves you wondering whether a quiet course was missed or genuinely has
    nothing. This returns the full roster with a count, so the UI can show
    "American Poetry — no assignments posted yet" instead of silence.
    """
    from datetime import datetime, timezone

    if current_term is None:
        current_term = current_term_name()

    try:
        from .onboarding import load_config
        cfg = load_config()
    except Exception:
        cfg = {}
    tutoring = [t.lower().strip() for t in (cfg.get("tutoring_courses") or []) if t.strip()]
    excluded = [e.lower().strip() for e in (cfg.get("excluded_courses") or []) if e.strip()]

    now = datetime.now(timezone.utc)
    out = []

    _taking = taking_list()

    for course in fetch_active_courses():
        name = course.get("name") or ""
        if not is_academic_course(course):
            continue

        if _taking:
            # Student named their classes. Show those, plus current-term
            # courses where Canvas says they teach — that's the whole picture.
            _tut = []
            try:
                from .onboarding import load_config
                _tut = [t.lower().strip()
                        for t in (load_config().get("tutoring_courses") or [])
                        if t.strip()]
            except Exception:
                pass
            _n = (course.get("name") or "").lower()
            # Onboarding picks are the only source of truth. Canvas roles
            # drag in stale courses from past terms where the student still
            # holds a teacher enrolment, so we do not consult them here.
            if not (is_taking(course) or any(t in _n for t in _tut)):
                continue
        elif not in_term(course, current_term):
            continue
        if any(e in name.lower() for e in excluded):
            continue

        if taking_list():
            teaches = not is_taking(course)
        else:
            teaches = is_teaching_role(course) or any(
                t in name.lower() for t in tutoring
            )

        try:
            assigns = fetch_assignments(course["id"]) or []
        except Exception:
            assigns = []

        upcoming = 0
        for a in assigns:
            raw = a.get("due_at")
            if not raw:
                continue
            try:
                if _parse_due(raw) > now:
                    upcoming += 1
            except Exception:
                pass

        out.append({
            "course": name,
            "work_type": "teaching" if teaches else "coursework",
            "total_assignments": len(assigns),
            "upcoming": upcoming,
            "has_work": upcoming > 0,
        })

    return out


# ---------------------------------------------------------------------------
# Role detection
# ---------------------------------------------------------------------------

TEACHING_ROLES = {"ta", "teacher", "designer", "TaEnrollment", "TeacherEnrollment"}


def course_role(course: dict) -> str:
    """The user's role in a course: 'student', 'ta', 'teacher', etc."""
    roles = [e.get("type", "") for e in (course.get("enrollments") or [])]
    for r in roles:
        if r in TEACHING_ROLES or r.lower().replace("enrollment", "") in TEACHING_ROLES:
            return r.lower().replace("enrollment", "") or r
    return roles[0].lower().replace("enrollment", "") if roles else "unknown"


def is_teaching_role(course: dict) -> bool:
    """True if the user teaches/TAs this course rather than taking it."""
    return course_role(course) in TEACHING_ROLES


def fetch_assignments(course_id: int) -> list[dict]:
    """All assignments for a course, including point values."""
    return _get(f"/courses/{course_id}/assignments", params={"per_page": 100})


def _parse_due(raw: str | None):
    """Canvas returns UTC. Convert to local so a 7pm Pacific deadline reads
    as that evening rather than 2am the next day."""
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()


def assignments_to_tasks(skip_teaching: bool = False) -> list[Task]:
    """Pull every course's assignments and normalize to Task objects.

    Skips assignments with no due date and ones already past due.

    Courses where Canvas says you're a TA/teacher — or that you named as
    tutoring in onboarding — are KEPT but tagged work_type="teaching".
    Grading and prep are real work, just a different kind than your own
    coursework. Non-academic shells (mandatory trainings, orientations) are
    dropped entirely.
    """
    tasks: list[Task] = []
    teaching_courses: list[str] = []
    now = datetime.now(timezone.utc)

    try:
        from .onboarding import load_config
        _tutoring = [t.lower().strip()
                     for t in (load_config().get("tutoring_courses") or [])
                     if t.strip()]
    except Exception:
        _tutoring = []

    for course in fetch_active_courses():
        course_id = course.get("id")
        course_name = course.get("name") or course.get("course_code") or str(course_id)

        if not is_academic_course(course):
            continue  # training / orientation shells aren't coursework

        # The student named their own classes; everything else current is
        # teaching/tutoring work.
        _taking = taking_list()
        if _taking:
            teaches = not is_taking(course)
        else:
            teaches = is_teaching_role(course) or any(
                t in course_name.lower() for t in _tutoring
            )
        if teaches:
            why = course_role(course) if is_teaching_role(course) else "you tutor it"
            teaching_courses.append(f"{course_name} ({why})")
            if skip_teaching:
                continue

        try:
            assigns = fetch_assignments(course_id)
        except Exception:
            continue

        for a in assigns or []:
            due = _parse_due(a.get("due_at"))
            if due is None or due < now:
                continue

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
