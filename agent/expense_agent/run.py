"""Autonomous background runner.

Keeps the agent's outputs fresh without you typing anything. Refreshes on a
loop and also serves the interface, so one command gets you a working system.

    uv run python -m expense_agent.run

What it does on each cycle:
  1. pulls Canvas assignments + module materials
  2. merges verified syllabus assignments
  3. rescores everything against your onboarding preferences
  4. rewrites daily_view.json / task_list.json (the UI picks this up on its own)

The calendar rebuild runs on a slower cadence, since wiping and rewriting
events every few minutes is wasteful and hammers the Google API.

Flags:
    --interval N     minutes between refreshes (default 15)
    --calendar N     minutes between calendar rebuilds (default 180)
    --port N         port for the web interface (default 8000)
    --no-serve       skip the web server, just refresh data
    --once           run one cycle and exit
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import http.server
import socketserver
import threading
import time
import traceback
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent


def _stamp() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


_last_task_count = {"n": None}


def refresh_data(verbose: bool = True) -> dict | None:
    """One data refresh: tasks, materials, scoring, JSON outputs."""
    from .canvas_poller import assignments_to_tasks
    from .onboarding import load_config
    from .daily_view import build_daily_view
    from .taskmaster_calendar import (
        _budget_hours, _dedupe_key, _is_excluded, _is_priority_course, _rank_value,
    )

    try:
        from .syllabus import syllabus_tasks
    except Exception:
        def syllabus_tasks():
            return []

    try:
        from .course_site import site_tasks
    except Exception:
        def site_tasks():
            return []

    try:
        from .materials import fetch_materials
    except Exception:
        def fetch_materials():
            return []

    cfg = load_config()

    canvas = assignments_to_tasks()
    seen = {_dedupe_key(t) for t in canvas}
    extra = [t for t in (list(syllabus_tasks()) + list(site_tasks()))
             if _dedupe_key(t) not in seen]
    tasks = [t for t in (canvas + extra) if not _is_excluded(t, cfg)]

    briefing = []
    for t in tasks:
        t.priority_score = _rank_value(t, cfg)
        briefing.append({
            "title": t.title,
            "course": t.course,
            "due": f"{t.due_at.astimezone():%a %b %d %I:%M %p}",
            "_due_dt": t.due_at.astimezone(),
            "rank": t.priority_score,
            "budgeted_hours": _budget_hours(t, cfg),
            "from_syllabus": t.source == "syllabus",
            "priority_course": _is_priority_course(t, cfg),
            "work_type": getattr(t, "work_type", "coursework"),
        })

    materials = fetch_materials()
    view = build_daily_view(briefing, cfg, materials=materials)

    if verbose:
        n_active = len(view.get("active", []))
        n_mats = len(view.get("materials", []))
        print(f"  [{_stamp()}] refreshed · {len(tasks)} tasks · "
              f"{n_active} active today · {n_mats} materials")
    return view


def rebuild_calendar(verbose: bool = True) -> None:
    """Slower cadence: rewrite the Taskmaster calendar."""
    try:
        from .taskmaster_calendar import rebuild_calendar_and_brief
        from .task_list import write_task_list
        brief, skipped, cfg = rebuild_calendar_and_brief()
        write_task_list(brief, skipped, cfg)
        if verbose:
            print(f"  [{_stamp()}] calendar rebuilt · {len(brief)} task(s) scheduled")
    except Exception as e:
        if verbose:
            print(f"  [{_stamp()}] calendar rebuild skipped: {str(e)[:70]}")


def _serve(port: int) -> None:
    """Serve the interface plus the /ask endpoint for the voice agent."""
    import json as _json

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass  # don't spam the console with every poll

        def _send_json(self, obj, code=200):
            payload = _json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            route = self.path.rstrip("/")

            # --- syllabus upload: raw file bytes, filename in a header ---
            if route == "/upload-syllabus":
                try:
                    import base64 as _b64
                    from .manual_sources import SYLLABI_DIR
                    length = int(self.headers.get("Content-Length", 0))
                    body = _json.loads(self.rfile.read(length) or b"{}")
                    name = (body.get("filename") or "").strip()
                    data_b64 = body.get("data") or ""
                    # keep the filename simple; it's used to match a course
                    name = "".join(c for c in name
                                   if c.isalnum() or c in " .-_()").strip()
                    if not name or not data_b64:
                        return self._send_json({"error": "missing file"}, 400)
                    SYLLABI_DIR.mkdir(exist_ok=True)
                    raw = _b64.b64decode(data_b64.split(",")[-1])
                    if len(raw) > 12 * 1024 * 1024:
                        return self._send_json({"error": "file too large (12MB max)"}, 400)
                    (SYLLABI_DIR / name).write_bytes(raw)
                    print(f"  [{_stamp()}] syllabus uploaded: {name} "
                          f"({round(len(raw)/1024)} KB)")
                    refresh_data(verbose=False)
                    return self._send_json({"ok": True, "file": name})
                except Exception as e:
                    return self._send_json({"error": str(e)[:120]}, 500)

            # --- save a course website URL ---
            if route == "/save-url":
                try:
                    from .manual_sources import save_course_url
                    length = int(self.headers.get("Content-Length", 0))
                    body = _json.loads(self.rfile.read(length) or b"{}")
                    course = (body.get("course") or "").strip()
                    url = (body.get("url") or "").strip()
                    if not course or not url:
                        return self._send_json({"error": "need course and url"}, 400)
                    if not url.startswith(("http://", "https://")):
                        url = "https://" + url
                    save_course_url(course, url)
                    print(f"  [{_stamp()}] course URL saved: {course[:30]}")
                    refresh_data(verbose=False)
                    return self._send_json({"ok": True})
                except Exception as e:
                    return self._send_json({"error": str(e)[:120]}, 500)

            if route != "/ask":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = _json.loads(self.rfile.read(length) or b"{}")
                question = (body.get("question") or "").strip()
                if not question:
                    result = {"answer": "I didn't catch that.", "used_context": False}
                else:
                    from .voice import ask
                    result = ask(question)
                    print(f"  [{_stamp()}] asked: {question[:60]}")
            except Exception as e:
                result = {"answer": "Something went wrong on my end.",
                          "error": str(e)[:120]}
            payload = _json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    import os as _os
    _STATIC_ROOT = _os.environ.get('STATIC_DIR') or str(_AGENT_ROOT)
    handler = functools.partial(Handler, directory=_STATIC_ROOT)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass  # browser closed early; not worth a traceback

    with Server(("", port), handler) as httpd:
        httpd.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the taskmaster continuously.")
    ap.add_argument("--interval", type=int, default=15, help="minutes between refreshes")
    ap.add_argument("--calendar", type=int, default=180, help="minutes between calendar rebuilds")
    ap.add_argument("--port", type=int, default=8000, help="port for the interface")
    ap.add_argument("--no-serve", action="store_true", help="don't start the web server")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = ap.parse_args()

    print("\n" + "=" * 62)
    print("  TASKMASTER — running")
    print("=" * 62)

    if not args.no_serve and not args.once:
        threading.Thread(target=_serve, args=(args.port,), daemon=True).start()
        print(f"  Interface:  http://localhost:{args.port}")
    print(f"  Refresh:    every {args.interval} min")
    print(f"  Calendar:   every {args.calendar} min")
    print("  Stop:       Ctrl+C")
    print("=" * 62 + "\n")

    if args.once:
        refresh_data()
        rebuild_calendar()
        return

    last_calendar = 0.0
    try:
        while True:
            try:
                refresh_data()
                now = time.time()
                if now - last_calendar >= args.calendar * 60:
                    rebuild_calendar()
                    last_calendar = now
            except Exception:
                print(f"  [{_stamp()}] refresh failed:")
                traceback.print_exc(limit=1)
            time.sleep(args.interval * 60)
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
