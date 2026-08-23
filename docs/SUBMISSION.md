# Submission checklist

Devpost form for the All Things Agentic Hackathon. Deadline **2026-08-31,
17:00 PT / 20:00 ET**. Submit on the 30th — the rules disclaim responsibility
for site malfunctions, and deadline-day load on Devpost is a real risk.

## Required fields

| Field | Value | Ready |
|---|---|---|
| **Category** | The Fortified Enterprise Fleet | ☐ |
| **Repository URL** | `https://github.com/Rickygole/overturn` — public, so no access grant needed | ☑ |
| **Hosted project URL** | The approval UI `.run.app` URL from `infra/deploy.sh` output | ☐ |
| **Text description** | Four parts, all drafted in [`PITCH.md`](PITCH.md) | ☑ |
| **Spin-up instructions** | In `README.md`, verified from a clean clone on 2026-08-23 | ☑ |
| **Architecture diagram** | [`architecture.svg`](architecture.svg), embedded in [`ARCHITECTURE.md`](ARCHITECTURE.md) | ☐ |
| **Demo video** | ≤4 min, **public** on YouTube, captions on | ☐ |

### The four text-description parts

Copy from `PITCH.md`, which has each written out:

1. **Features and functionality** — the seven agents and what sits underneath them
2. **Technologies used** — see the table below
3. **Other data sources used** — Synthea for the patient base; the payer, its
   policies and every denial letter are authored for this project. Say plainly
   that no real patient data and no real insurer's name appear anywhere.
4. **Findings and learnings** — the three genuine ones, written up in `PITCH.md`

### Technologies, for the "technologies used" field

- **Gemini 3.5 Flash and Gemini 3.7 Flash** via Vertex AI — every generative call
  is at or above the required 3.5 floor. `docs/MODEL_CHOICES.md` records why
  there is no Pro-tier model in the assignment.
- **Gemma** (`gemma-4-26b-a4b-it-maas`) — Sentinel's open-weights screening layer.
  **Mention this explicitly in the submission text**, since the additional-Google-model
  bonus is worth 0.2 and a judge has to notice it to award it.
- **Google ADK** — the seven agent definitions, and `AdkBackend` executes every
  generative call through an ADK `LlmAgent` and `Runner`.
- **Model Armor** — Sentinel's inline guardrail against prompt injection.
- **Cloud Run** (three services), **Pub/Sub** (with a dead-letter topic),
  **Cloud Scheduler**, **Firestore**, **Cloud Storage**, **Cloud Trace**,
  **Secret Manager**, **IAM** (eight service accounts).
- **OpenTelemetry** — every agent invocation is a span; the drafting retries nest
  under one parent.

## Bonus contributions

Each is worth up to 0.2 on a 6-point scale, where category placement will
separate by tenths. Skipping them is irrational.

| Bonus | Status |
|---|---|
| Additional Google AI model — Gemma in Sentinel | ☑ built; must be **named in the submission text** |
| Blog post, public, on dev.to | ☐ drafted in [`BLOG_POST.md`](BLOG_POST.md); flip `published: true` |
| Social post with `#AllThingsAgenticHackathon` | ☐ drafted in [`PITCH.md`](PITCH.md) |

The blog post must contain language stating it was created for the purposes of
entering this hackathon, and must be **public, not unlisted**. Both are already
in the draft.

## Before recording the video

- [ ] `bash infra/provision.sh && bash infra/deploy.sh` — see `docs/RUNBOOK.md`
- [ ] Seed the agent registry: `uv run python scripts/seed_registry.py --publish`
- [ ] Confirm every frame shows **Northbeck Health Plan** and no real insurer's
      name. Grep before uploading: `grep -ric "meridian" data/ docs/`
- [ ] Say out loud, in the first 20 seconds, that the patients, the payer and its
      policies are all synthetic, and that a person approves before anything is sent
- [ ] Say out loud that the payer response window is compressed for the demo
- [ ] Have the Cloud Run console, a `.run.app` URL and a Cloud Trace view ready
      to show — the rules require visible proof of Google Cloud deployment
- [ ] Terminal font large enough to read at 1080p

Shot list and narration timings are in [`VIDEO_SCRIPT.md`](VIDEO_SCRIPT.md).

## After submitting

- [ ] Record a backup screen capture of the system running on Cloud Console
- [ ] `bash infra/teardown.sh` — the rules say the project need not be live at
      judging, and there is no reason to bleed credits until October
- [ ] Filter for Devpost and Google Cloud mail. Winners are announced around
      2026-10-08 and there are **two days** to respond before the prize moves to
      the next entrant

## What is deliberately not claimed

Worth re-reading before writing the submission text, because the fastest way to
lose is to overstate something a judge then checks:

- Retrieval is TF-IDF with order-independent word pairs, **not** vector search.
  A vector path exists in the design and is not what runs.
- Agent Registry, Agent Gateway and Memory Bank are **our own primitives**, not
  the managed GEAP services — no public REST surface was reachable on this
  project. `docs/PLATFORM_PROBE.md` records the probe and `ARCHITECTURE.md`
  carries the mapping table.
- Agent Runtime is not used; the pipeline terminates at a human gate and resumes
  from Firestore, which is not what a managed runtime session is for.
- The payer endpoint is simulated. Nothing is transmitted to a real insurer.
- Demo mode compresses the response window. Disclosed in the README, in the
  video, and on screen.
