"""Make the agent Cloud Run compatible.

Two things differ in a container:

  1. Cloud Run tells you which port to listen on via $PORT (8080), and will
     kill the container if nothing is listening there.
  2. The filesystem is ephemeral. Anything written to disk vanishes when the
     instance restarts or scales to zero, so state goes to $STATE_DIR, which
     deploy.sh points at a mounted Cloud Storage bucket.

Run from the agent directory:  python3 deploy/make_cloud_ready.py
"""

import pathlib
import re
import sys

AGENT = pathlib.Path("expense_agent")
if not AGENT.is_dir():
    sys.exit("run this from the agent directory (the one containing expense_agent/)")

changed = []

# --- 1. listen on $PORT -----------------------------------------------------
p = AGENT / "run.py"
s = p.read_text()
if "PORT" not in s:
    s = s.replace(
        "def main():",
        'def _port() -> int:\n'
        '    """Cloud Run supplies $PORT and health-checks it. Local default 8000."""\n'
        '    import os\n'
        '    return int(os.environ.get("PORT", 8000))\n'
        '\n'
        '\n'
        'def main():',
        1,
    )
    s = re.sub(r'\bPORT\s*=\s*8000\b', 'PORT = _port()', s)
    s = re.sub(r'serve\(\s*8000\s*\)', 'serve(_port())', s)
    s = re.sub(r'port\s*=\s*8000', 'port = _port()', s)
    p.write_text(s)
    changed.append("run.py listens on $PORT")

# --- 2. state goes to $STATE_DIR -------------------------------------------
# Every module resolves paths from _AGENT_ROOT; point that at STATE_DIR when
# the variable is set, so a single change moves all state onto the bucket.
for name in ("daily_view.py", "task_list.py", "calendar_view.py",
             "manual_sources.py", "course_site.py", "materials.py",
             "syllabus.py", "onboarding.py", "voice.py"):
    f = AGENT / name
    if not f.exists():
        continue
    s = f.read_text()
    if "_AGENT_ROOT" in s and "STATE_DIR" not in s:
        s = s.replace(
            "_AGENT_ROOT = Path(__file__).resolve().parent.parent",
            "import os as _os\n"
            "_AGENT_ROOT = Path(_os.environ.get('STATE_DIR')) \\\n"
            "    if _os.environ.get('STATE_DIR') \\\n"
            "    else Path(__file__).resolve().parent.parent",
        )
        f.write_text(s)
        changed.append(f"{name} writes to $STATE_DIR")

# --- 3. serve index.html from the image, not the state dir ------------------
p = AGENT / "run.py"
s = p.read_text()
if "_STATIC_ROOT" not in s:
    s = s.replace(
        "handler = functools.partial(Handler, directory=str(_AGENT_ROOT))",
        "import os as _os\n"
        "    _STATIC_ROOT = _os.environ.get('STATIC_DIR') or str(_AGENT_ROOT)\n"
        "    handler = functools.partial(Handler, directory=_STATIC_ROOT)",
    )
    p.write_text(s)
    changed.append("run.py serves static files from $STATIC_DIR")

print("\nCloud Run prep:")
for c in changed:
    print(f"  - {c}")
if not changed:
    print("  (already applied)")
print()
