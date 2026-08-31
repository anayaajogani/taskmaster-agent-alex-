# Knudge 

An agent that finds your coursework wherever it lives, works out what's urgent,
and puts it on your calendar autonomously before you fall behind.

Built for the All Things Agentic hackathon (Category 1) using the Google Agent
Development Kit, Gemini, and Google Cloud Run.

---

## The problem

What we realised was that students don't miss deadlines because they're careless. They miss them because
the work isn't all in one place.

A single semester's assignments are spread across bCourses, a syllabus PDF, a
course website, Gradescope, and Ed announcements. Nothing reconciles those, so
staying on top of your own coursework means checking five places and holding the
rest in your head. The first time you find out about a deadline is often after
you've missed it.

We ran into this ourselves in our everyday lives, but we wanted to be sure it wasn't just us before
building anything. It isn't. It's structural, and it affects any student whose
courses don't all live in the same system.

## What existing tools miss

Every planner and study app we'd tried had the same gap: none of them talk to
bCourses. You get a to-do list that doesn't know about your actual assignments,
so you copy deadlines across by hand, which is the work you were trying to
avoid.

The tools that do connect to a learning management system stop there, and that
covers maybe half of a real course load. The midterm that started this project
wasn't on Canvas at all. It was three pages into a syllabus PDF, in a paragraph
about grading weights. Canvas listed two assignments for that class. The
syllabus had seven, including both midterms and every research project
milestone.

Data science courses are the clearest case. They run off their own websites,
hand out work through Gradescope, and post announcements on Ed, with Canvas
barely used. A student in those classes gets almost nothing from a Canvas-only
tool.

So the goal was to pull the fragmented pieces into one place and turn them into
something actionable, a calendar you can follow, rather than another list to
maintain. And to build it for any student with this problem, not just for
ourselves.

## What students told us

We surveyed 64 Berkeley students to check the problem generalized.

**All 8 said they'd use a tool like this.** Five said definitely, three said
maybe, none said no.

**Half had already missed or been late on an assignment** because they forgot
about it or never saw it.

**Nobody agreed on how to prioritize.** Three go by what's worth more of their
grade, three by what takes longest, two by what's due soonest. That split is why
Taskmaster asks during setup instead of deciding for you.

One response confirmed the fragmentation problem directly:

> "Not everything is on Canvas (CS & Math classes don't use it). Most stuff is
> in the syllabus/gradescope/Ed announcements"

The rest were about trust:

> "As long as it creates a new category on my calendar, I'll trust it"

> "It asks permission before changing stuff"

> "I can easily configure rules around when it can and can't schedule stuff.
> Also it has enough context to actually decide how hard something is"

> "If it's too complicated to use and messes my assignments I'd rather just
> manually input it"

Those became features. Taskmaster creates a separate calendar it owns and never
touches your existing events. It explains what it's about to do and waits for
you to agree before its first calendar write. It asks about your hours, off-days
and daily limit during setup, so you set the rules.

That last quote is the one we kept coming back to. A tool that gets your
schedule wrong is worse than no tool, because you stop trusting it and go back
to doing it by hand. That's why nothing gets scheduled unless it traces to a
real line in a real document.

---

## What it does

Reads assignments from three places and merges them: the Canvas API, syllabus
PDFs you drop in, and course websites for classes that don't use Canvas.

Separates courses you take from ones you TA or tutor. Teaching work still gets
scheduled, since grading takes real hours, but it's ranked on urgency alone.
"Worth more of my grade" means nothing for a class you grade.

Ranks by whatever you told it to care about, then schedules the work in blocks
that fit your stated hours and daily cap, on a calendar it created for itself.

Answers questions out loud from your actual task data, and says "that isn't in
what I can see" rather than making something up.

Keeps itself current. Refreshes every fifteen minutes and rebuilds the calendar
when your workload changes.

## How it avoids inventing work

This was the hard part. An LLM asked to read a course webpage will produce
assignments that look right and don't exist, which is exactly the failure that
makes a student stop trusting the tool.

We used two different approaches depending on the source.

**Course websites are parsed with regex, no model involved.** Academic sites
follow predictable table layouts, so a date can only come from the row it was
found in and a title only from its label. There's no step where something could
be fabricated. When a page doesn't match a known layout, it says so instead of
guessing.

**Syllabi need Gemini**, because they're unstructured prose. Every assignment
the model claims to find has to quote the exact sentence it came from, and that
quote gets checked against the source text. Anything that fails verification is
dropped before it reaches your schedule.

The same split runs through the system. Gemini handles judgment calls like
estimating how long something will take. Deterministic code handles urgency
scoring, calendar placement and capacity limits, so the behaviour is predictable
and testable.

---

## Architecture

```
Canvas API ─┐
Syllabus PDF ├─→ normalize → score → schedule ─→ Google Calendar
Course site ─┘      │          │                 Web interface
                    │          │                 Voice agent
              Gemini for       Deterministic
              judgment calls   business rules
```

Python 3.12, Google ADK, Gemini via Vertex API, Canvas LMS API, Google Calendar
API, Cloud Run, Cloud Storage, Secret Manager.

---

## Running it locally

You'll need Python 3.12+, [uv](https://docs.astral.sh/uv/), a Canvas account at
a school that allows API tokens, and a Vertex API Key. The free tier is enough.

### Install

```bash
git clone https://github.com/anayaajogani/taskmaster-agent-alex-.git
cd taskmaster-agent-alex-/agent
uv sync
```

### Credentials

Create a file called `.env` in the `agent/` directory:

```bash
GOOGLE_API_KEY=your_gemini_key
CANVAS_TOKEN=your_canvas_token
CANVAS_BASE_URL=https://bcourses.berkeley.edu
```

Get a Canvas token from **Account → Settings → New Access Token**. They expire,
so if you start seeing 401 errors later, then generate a new one.

### Google Calendar (optional)

Calendar scheduling needs OAuth credentials. In the
[Google Cloud Console](https://console.cloud.google.com), create a project,
enable the Google Calendar API, create an OAuth 2.0 Client ID of type **Desktop
app**, then download the JSON and save it as `agent/gcal_credentials.json`.

The first run opens a browser to authorize. Before it asks, the agent prints
what it will and won't do. It creates a calendar called "Taskmaster" and only
writes there.

Skip this and everything else still works, you just won't get calendar blocks.

### Setup

```bash
uv run python -m expense_agent.onboarding
```

It pulls your Canvas enrolments, lists them, and asks which you're taking and
which you TA or tutor.

We tried inferring this automatically first. Canvas returns dozens of courses
across every term you've ever enrolled in, with inconsistent naming and
unreliable term data, and Berkeley reports "Default Term" for everything. Every
heuristic we tried either dropped real classes or pulled in mandatory training
modules. Asking once is more reliable than any amount of guessing.

You'll also set your priority mode, working hours, daily cap and lead time.

### Run

```bash
uv run python -m expense_agent.run
```

Open **http://localhost:8000**.

### Adding classes that aren't on Canvas

At the bottom of the page there's a drop zone and a URL field.

Drop a syllabus PDF named after the course, like `Data-89.pdf`, and Gemini
extracts the assignments with the verification described above.

Save a course website by picking the course and pasting the URL. Sites built on
common academic templates get parsed for their assignment calendar. You can also
scrape manually:

```bash
uv run python -m expense_agent.course_site
```

---

## Deploying to Cloud Run

You'll need a GCP project with billing enabled and the
[gcloud CLI](https://cloud.google.com/sdk/docs/install) authenticated:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

### Prepare the code

Cloud Run containers get an ephemeral filesystem and an injected `$PORT`. This
handles both:

```bash
cd agent
python3 deploy/make_cloud_ready.py
```

The server binds to `$PORT`, and everything the agent writes goes to
`$STATE_DIR`, which the deploy script points at a Cloud Storage bucket so your
data survives restarts and scale-to-zero.

### Deploy

Set `PROJECT` and `REGION` at the top of `deploy/deploy.sh`, then:

```bash
bash deploy/deploy.sh
```

It enables the required APIs, creates the state bucket, uploads your existing
config, stores your Canvas token and Gemini key in Secret Manager, builds the
container and deploys it. First run takes three to five minutes and prints your
public URL at the end.

### Grant secret access

Cloud Run's service account needs permission to read those secrets:

```bash
PROJECT=YOUR_PROJECT_ID
SA=$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com
for s in canvas-token gemini-key; do
  gcloud secrets add-iam-policy-binding $s \
    --member="serviceAccount:$SA" \
    --role="roles/secretmanager.secretAccessor" --project=$PROJECT
done
```

### Check it worked

```bash
gcloud run services logs read taskmaster --region us-west1 --limit 30
gcloud storage ls gs://YOUR_PROJECT_ID-state/
```

Once the first refresh finishes, `daily_view.json` should be in the bucket.

### What's different in the cloud

Google Calendar writing needs a browser OAuth flow, which a container can't do.
The deployed version skips calendar writes and runs everything else: Canvas
polling, syllabus reading, site scraping, the web interface and voice. Calendar
scheduling works when you run it locally.

### Tearing it down

```bash
gcloud run services delete taskmaster --region us-west1
gcloud storage rm -r gs://YOUR_PROJECT_ID-state
```

---

## Layout

```
agent/expense_agent/
  run.py                 entry point, web server and refresh loop
  agent.py               ADK workflow graph
  canvas_poller.py       Canvas API, role detection, course selection
  syllabus.py            Gemini syllabus extraction with verification
  course_site.py         deterministic course website parser
  taskmaster_calendar.py scheduling and calendar writes
  scoring.py             urgency and grade impact ranking
  study_plan.py          capacity-aware daily planning
  daily_view.py          builds what the interface renders
  voice.py               grounded voice question answering
  onboarding.py          first-run course selection
  manual_sources.py      syllabus uploads and saved course URLs
index.html               single-file React interface
deploy/                  Dockerfile, deploy script, cloud prep
```

## What doesn't work yet

Google Calendar needs local OAuth, so it isn't available in the deployed
version.

Course website parsing covers common academic templates. Calendars rendered by
JavaScript return nothing, and the agent tells you that rather than pretending
it found something.

Canvas tokens expire every few months and have to be regenerated by hand.

Free-tier Gemini rate limits can make voice answers take several seconds.

The survey was 8 students at one university. Enough to tell us the problem was
real and to shape what we built, not enough to call representative.

## Team

Anayaa Jogani ([@anayaajogani](https://github.com/anayaajogani)) ·
Kunal Baldava ([@kvnalb](https://github.com/kvnalb))
