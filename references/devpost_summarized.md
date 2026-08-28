# All Things Agentic — Taskmaster build and submission brief

Source: [official Devpost site](https://allthingsagentichackathon.devpost.com/) and its linked rules, resources, FAQ, and organizer updates. Condensed on August 27, 2026 for a three-day build.

This brief is tailored to the draft **StudyAgent** PRD: an autonomous student coordinator that turns Canvas/Ed updates into protected study time, relevant resources, and concise briefings.

## The target

Taskmaster is not a chatbot track. Build an event-driven agent that notices a real change, decides what should happen next, and completes a messy multi-step workflow across tools without the user directing every step.

The strongest framing is “bring your own friction”: solve a specific chore that someone on the team genuinely experiences. The demo should make this chain undeniable:

`real trigger → autonomous plan/routing → 2–3 consequential tool actions → persisted result/audit trail`

Examples from the organizers include turning meeting transcripts into tasks plus a Slack summary, or handling a freelance inquiry by checking a calendar, using past work, and preparing a proposal. Copy the shape, not the example.

For a three-day build, the winning StudyAgent scope is one golden workflow:

`new/changed assignment → estimate effort and urgency → inspect calendar → create or move study blocks → persist an audit trail → send/show the changed plan`

This makes the Autonomous Time-Blocker the product. The Executive Briefing should be a compact output of the same workflow, not a second subsystem. Treat the Deep Work Resource Bundler as stretch scope; a polished bundle is useful, but it is less valuable than proving autonomous calendar action and rescheduling.

Do not build a general-purpose assistant, multi-agent platform, broad settings system, full-semester simulator, complex RAG stack, or production OAuth onboarding in the three-day MVP. Preconfigure one test student account and start the demo already authenticated.

## Mandatory technical floor

Every submission must use all three:

1. Gemini 3.5 or newer, through the Gemini API or Vertex AI.
2. At least one Google agent framework: Google ADK, GenAI SDK, Antigravity SDK, or Genkit.
3. At least one Google Cloud infrastructure service, such as Cloud Run, Firestore, Pub/Sub, Cloud SQL, or GKE.

The project must be new and built during the submission period. Select exactly one track: Taskmaster.

## Sensible default implementation

Use this StudyAgent architecture:

- Python service using Google ADK, with Gemini 3.5 Flash via Vertex AI.
- A small FastAPI service on Cloud Run; minimum instances `0`.
- Firestore for assignments, calendar-block mappings, run state, decisions, errors, and idempotency keys. Cloud Run is stateless; Firestore provides the long-running continuity.
- Cloud Scheduler calls a `/sync` endpoint for the background-work proof. Also provide a “simulate sync” button so the demo never depends on a timer.
- One Canvas/Ed ingestion adapter. For the MVP, accept a realistic fixture or exported payload if live APIs/authentication become a time sink; the judging notes explicitly allow synthetic data when the logic is real. Label it honestly.
- Google Calendar API as the real consequential action: create study blocks, detect a newly added conflict, and move affected blocks.
- Optional email/briefing output only after calendar automation works. A dashboard card is sufficient if email delivery introduces risk.
- A tiny Focus dashboard showing assignment urgency/“spiciness,” current study blocks, and the Agent Reflection log. The UI exists to make autonomous actions legible to judges.

Keep the workflow explicit and inspectable. A practical run state machine is:

`assignment_detected → effort_estimated → calendar_checked → blocks_written → briefing_updated → completed | needs_attention | failed`

Store every state transition. Give external writes deterministic idempotency keys so retries do not create duplicate emails, tickets, purchases, or records. Put a timeout and bounded retry around each tool. Never let Gemini call arbitrary functions: expose a short allowlist with narrow input schemas. Keep credentials in environment variables or Secret Manager, never in prompts, logs, screenshots, or the repository.

Calendar writes are reversible, so let StudyAgent perform them autonomously and expose undo plus an audit log. Require approval only for destructive or external-facing actions added later. This keeps the Taskmaster proof strong without ignoring safety.

## What judges optimize for

Stage one is pass/fail: the entry must include every required artifact, address the track, and meet the required stack. Missing logistics can eliminate a good build.

### 1. Innovation and operational utility — 40%

- Removes real-world friction rather than answering questions.
- Performs high-value action with little or no hand-holding.
- Taskmaster-specific: intercepts and completes a multi-step background workflow without human intervention.
- Solves a distinctive, personal “bring your own friction” problem.

Optimize StudyAgent by measuring a before/after outcome: five source/calendar checks reduced to one autonomous sync; minutes of weekly planning saved; assignment changes caught; or scheduling conflicts resolved. Show a real Calendar write and persisted state—not only dashboard mockups or narrated claims.

### 2. Architectural discipline and tech stack — 30%

- Clean separation between orchestration, model reasoning, tools, and persistence.
- State management, scoped credentials/tools, failure handling, retries, and maintainability.
- Production-minded behavior rather than a brittle happy-path script.

Optimize by implementing one visible failure/recovery path and documenting it. A small reliable system scores better than a sprawling unfinished one.

### 3. Demo and production readiness — 30%

- Clear four-minute video that defines the friction and explains the architecture.
- Unedited live execution of the agent, evidenced by logs, database/UI changes, or messages sent.
- Clean architecture diagram and reproducible README setup.
- Visible proof that the backend was deployed on Google Cloud.

Judges are not required to download or run the project. They may score from the video, description, and repository alone. Prioritize those surfaces.

## Three-day execution plan

### Day 1 — make the calendar loop real

- Freeze the hero scenario: a high-effort project appears, StudyAgent creates study blocks, then a social event forces a reschedule.
- Scaffold ADK + Vertex AI and expose narrow tools: read assignments, read availability, create/move calendar block, write run state.
- Persist assignments, block mappings, decisions, and tool outputs in Firestore.
- Get the end-to-end loop working locally against a real Google Calendar and deterministic academic fixture.

### Day 2 — deploy and make it credible

- Deploy the service to Cloud Run and wire Cloud Scheduler plus a manual demo trigger.
- Add idempotency so repeated syncs update existing blocks instead of duplicating them; add bounded retries, timeouts, and one visible recovery scenario.
- Build the minimal Focus dashboard and Agent Reflection log.
- If stable, derive the Executive Briefing from the run result. Do not start the Resource Bundler until this is solid.
- Capture early proof of the Cloud Run deployment, logs, and Firestore state.
- Write the architecture diagram while the design is still easy to change.

### Day 3 — submission is part of the product

- Stabilize; do not add major features.
- Rehearse the assignment-to-calendar workflow, conflict-driven reschedule, and one recovery/idempotency example.
- Finish README spin-up instructions and verify them from a clean environment.
- Record, edit, upload, and verify the public video early.
- Complete the Devpost form and test every link in an incognito/private window.
- Leave buffer for video processing, teammate acceptance, and upload failures.

## Four-minute demo plan

Only the first four minutes may be evaluated. The video must be publicly visible on YouTube or Vimeo and be in English or have English subtitles.

- **0:00–0:15 — proof first:** trigger a sync and immediately show a newly posted project becoming real study blocks in Google Calendar. Skip logos, biographies, onboarding, and setup.
- **0:15–0:40 — friction and value:** explain the Berkeley “five-tab tax” and quantify time spent checking Canvas/Ed and manually replanning.
- **0:40–1:55 — live golden path:** show the assignment payload, Gemini effort/urgency decision, free-time lookup, real Calendar writes, Firestore state, and Agent Reflection entries.
- **1:55–2:30 — long-running autonomy:** add a conflicting social event, run the next sync, and show StudyAgent move the affected block while preserving the deadline plan. Re-run once to prove idempotency/no duplicates.
- **2:30–2:55 — briefing/bundle:** show the updated executive briefing; show the resource bundle only if it is genuinely working.
- **2:55–3:20 — architecture:** show one clean diagram connecting the academic event source, Cloud Scheduler/Cloud Run, ADK + Gemini on Vertex AI, Firestore, Google Calendar, and the Focus dashboard.
- **3:15–3:35 — Google Cloud proof:** visibly show the Cloud Run dashboard or `.run` URL plus logs/Firestore evidence. This is required.
- **3:35–3:55 — outcome:** restate the measured planning burden removed and why StudyAgent is a Taskmaster: it notices, decides, writes, and replans without a chat prompt.
- **3:55–4:00 — stop.** Do not depend on anything after four minutes.

Record in short clips; cut loading and typing; start logged in; use one strong example; add on-screen labels for trigger, decision, calendar action, persisted state, and cloud proof. Upload early because processing can take hours. The pre-submission session recommends an energetic human voice and warns against AI voiceovers; use a real narrator if possible.

## Required submission checklist

- Category: Taskmaster.
- Hosted project URL, if available; hosting is strongly encouraged. Put test credentials in the submission instructions if gated.
- Written description covering features/functionality, technologies, other data sources, and findings/learnings.
- Public or private GitHub/GitLab/Bitbucket repository.
- If private, grant access to `testing@devpost.com` and `cloudhackathons@google.com` before the deadline.
- README with step-by-step local setup or cloud deployment instructions.
- Architecture diagram showing Gemini, framework/orchestrator, backend, state/database, frontend, integrations, and Google Cloud services.
- Public YouTube/Vimeo demo, maximum four evaluated minutes, in English or subtitled.
- Video must show the problem, value, application working, and proof that the backend runs on Google Cloud.
- State which Google SDK/framework was used and the project start date.
- Add every teammate and make sure each invitation is accepted.
- Verify all links from an incognito/private window.

The application does not need to stay publicly live throughout judging. Capture conclusive deployment proof in the video and repository, then scale down or switch off services to control cost.

## Deadline and lock rules

- Submission deadline: **August 31, 2026 at 5:00 PM Pacific**.
- Judging: **September 1 at 9:00 AM through September 24 at 5:00 PM Pacific**.
- Winners announced: **October 8, 2026 at 12:00 PM Pacific**.
- After the deadline, do not edit the submission, repository, video, or linked live materials until winners are announced; organizer guidance says even small changes can affect eligibility. If development must continue, fork the repository and work on the copy.

## Bonus work: only after the core is done

Stage three can add bonus points:

- Public build article/podcast/video that explicitly says it was created for entering this hackathon: up to `+0.2`.
- Public social post; use `#AllThingsAgenticHackathon` on X or LinkedIn: up to `+0.2`.
- Additional Google AI models such as Gemma, Veo, or Lyria: `+0.2` each, up to `+0.6`.

Do not spend core build time on these unless the golden workflow, reliability, repo, architecture diagram, and demo are already complete.

## Useful links only

- [Hackathon overview](https://allthingsagentichackathon.devpost.com/)
- [Official rules](https://allthingsagentichackathon.devpost.com/rules)
- [Official resources](https://allthingsagentichackathon.devpost.com/resources)
- [FAQ](https://allthingsagentichackathon.devpost.com/details/faqs)
- [Schedule](https://allthingsagentichackathon.devpost.com/details/dates)
- [Submission-form walkthrough](https://devpost.helpscoutdocs.com/article/122-how-to-enter-a-submission)
- [Google ADK documentation](https://google.github.io/adk-docs)
- [Google ADK Python repository](https://github.com/google/adk-python)
- [Gemini API and Google AI Studio](https://ai.google.dev)
- [Cloud Run](https://cloud.google.com/run)
- [Firestore](https://cloud.google.com/firestore)
- [Google Cloud free trial](https://cloud.google.com/free) — the current FAQ says the hackathon-specific $150 credit pool has run out.
- [Long-running agents workshop](https://cloudonair.withgoogle.com/events/build-long-running-agent-persistent-workflows-google-adk)
- [Official build-session Q&A](https://www.youtube.com/watch?v=DCXjvKmUIGY)
- [Devpost discussion board](https://allthingsagentichackathon.devpost.com/forum_topics)
- [Devpost Discord](https://discord.gg/HP4BhW3hnp)

## Prize context

The Taskmaster track prize is listed as **$20,000 USD + $2,000 Google Cloud credits**, virtual coffee with a Google team member, and social promotion. Taskmaster entries are also eligible for the overall grand prize and applicable cross-track awards. Build for the weighted criteria, not the prize list.
