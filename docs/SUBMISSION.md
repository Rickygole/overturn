# Submission checklist

Devpost form for the All Things Agentic Hackathon. Deadline **2026-08-31,
17:00 PT / 20:00 ET**. Submit on the 30th — the rules disclaim responsibility
for site malfunctions, and deadline-day load on Devpost is a real risk.

## Required fields

| Field | Value | Ready |
|---|---|---|
| **Category** | The Fortified Enterprise Fleet | ☐ |
| **Repository URL** | `https://github.com/Rickygole/overturn` — public, so no access grant needed | ☑ |
| **Hosted project URL** | `https://overturn-kruy6aauaq-uc.a.run.app` — password `northbeck-appeals-2026` (see "Testing notes for judges" below) | ☑ |
| **Text description** | Four parts, all drafted in [`PITCH.md`](PITCH.md) | ☑ |
| **Spin-up instructions** | In `README.md`, verified from a clean clone on 2026-08-23 | ☑ |
| **Architecture diagram** | Paste `https://overturn-kruy6aauaq-uc.a.run.app/architecture.svg` — a URL a judge can open from the form. The repo-relative link is not a substitute: the last scoresheet recorded "I cannot evaluate what I cannot open" | ☑ |
| **Multi-week asynchronous operation** | Paste `https://overturn-kruy6aauaq-uc.a.run.app/case/CASE-006` — a case that escalated itself to peer-to-peer review weeks after submission, unattended. The last scoresheet: "The track's defining claim is asserted and never shown... I click. There is nothing to see." Now there is, and the URL goes straight to it | ☑ |
| **Agent discovery / GEAP mapping** | Paste `https://overturn-kruy6aauaq-uc.a.run.app/system.html#geap` — what was probed on day one, what is managed, what is a primitive and why, for all seven GEAP components. The last scoresheet: "Agent discovery is absent." It was on the record in `ARCHITECTURE.md` and off the two pages a judge actually opens; it is now on one of them, at this anchor | ☑ |
| **Demo video** | ≤4 min, **public** on YouTube, captions on | ☐ |

**There is exactly one URL.** The site and the review queue are one Cloud Run
service: the public pages at `/`, the queue at `/queue` behind the app
password. `docs/*.html` is served by that process, not by GitHub Pages.

Pages was briefly enabled as a mirror and has been turned off deliberately, so
that no second address for this project exists to be found, indexed, or handed
to a judge alongside the real one. If it is ever re-enabled it becomes a second
copy of the same claims, drifting independently of the deployment -- which is
the exact failure this project has already had to clean up once.

### Testing notes for judges

Put this where the contest rules ask for testing instructions when a hosted
project isn't open to the public without one:

- **`https://overturn-kruy6aauaq-uc.a.run.app`** — the human approval
  interface. It is gated by a single shared password, **`northbeck-appeals-2026`**,
  not a Google account. This is deliberate and disclosed, not an oversight —
  see `services/approval_ui/auth.py`. Everything behind the login is synthetic:
  an invented payer, invented policies, generated patients.
- **Two links worth opening directly, not navigating to**:
  `.../case/CASE-006` — a case that escalated itself to peer-to-peer review
  weeks after submission with nobody watching, which is the multi-week
  asynchronous claim this track asks for, on the record rather than asserted;
  and `.../system.html#geap` — every GEAP component, what was probed, what is
  managed, what is a primitive and why.
- **`overturn-ingest` and `overturn-scheduler` are private by design** — Pub/Sub
  and Cloud Scheduler invoke them, not a browser. A **403 from either of their
  `.run.app` URLs is correct**, not a broken deployment. Point a judge at the
  approval UI, the Cloud Run console, or a Cloud Trace waterfall instead if
  they want to see them alive.
- The approval login is carried by `infra/deploy.sh` itself now, not by a
  separate step run after it. It reads the `overturn-ui-secret` secret from
  Secret Manager and appends `OVERTURN_UI_PASSWORD`/`OVERTURN_UI_SECRET` to
  the approval service's environment inside the same `gcloud run deploy`
  call, so re-running it no longer strips the login the way an earlier
  version of this script did. The one real precondition: the secret has to
  already exist. If it does not, `deploy.sh` deploys the approval service
  with no login configured — on purpose, since with no password set the app
  refuses to serve the queue at all rather than fail open. Run
  `infra/open_public.sh` once, first, if the secret has never been created.

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
- **Vertex AI Embeddings** (`text-embedding-005`) — reachable and exercised by
  `agents/retrieval/vector.py`, but **not on the retrieval path**. See "What is
  deliberately not claimed" below; do not list this as a technology the system
  depends on.
- **Model Armor** — Sentinel's inline guardrail against prompt injection.
  `docs/SCREENING_LAYERS.md` records the measured, slightly humbling result:
  Model Armor returned `NO_MATCH_FOUND` on the poisoned test letter and the
  deterministic rules layer is what actually caught it — worth citing in the
  text description as evidence the "defence in depth" claim is measured, not
  asserted.
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

- [ ] Run the full provisioning order once, not just `provision.sh` and
      `deploy.sh` — the second `iam_setup.sh` pass after `provision.sh` is
      required, not optional; see "One-time setup, in order" in
      `docs/RUNBOOK.md` for why and what breaks if it's skipped:
      `bash infra/enable_apis.sh && bash infra/iam_setup.sh && bash infra/provision.sh
      && bash infra/iam_setup.sh && bash infra/iam_audit.sh
      && INGEST_PUSH_PATH=/pubsub/push bash infra/deploy.sh`
- [ ] Seed the agent registry: `uv run python scripts/seed_registry.py --publish`
- [ ] Confirm every frame shows **Northbeck Health Plan** and no real insurer's
      name. This is enforced by a test rather than a checklist item:
      `uv run pytest tests/test_docs_accuracy.py -k payer`
- [ ] Say out loud, in the first 20 seconds, that the patients, the payer and its
      policies are all synthetic, and that a person approves before anything is sent
- [ ] Say out loud that the payer response window is compressed for the demo
- [ ] Have the Cloud Run console, a `.run.app` URL and a Cloud Trace view ready
      to show — the rules require visible proof of Google Cloud deployment
- [ ] Terminal font large enough to read at 1080p
- [ ] The two-signature gate is now fully clickable in the browser — clerk
      approval at `/case/{case_id}` and the clinician co-sign at
      `/case/{case_id}/clinical` — so the earlier blocker ("Approve" only
      reaches `approved`, not `submitted`) is closed. Confirm `VIDEO_SCRIPT.md`'s
      note about this has been resolved there before recording that beat.
- [ ] If showing the hosted approval UI rather than a local run, confirm the
      login actually works first: open the `.run.app` URL cold and log in with
      `northbeck-appeals-2026`. If it 403s instead of showing a login page, the
      manual step in `docs/RUNBOOK.md` ("The approval UI's login, and what
      `deploy.sh` doesn't do for it") needs to be reapplied — a redeploy since
      it was last set reverts it.

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

- **Retrieval is lexical TF-IDF, everywhere, including the deployed system.**
  A hybrid retriever exists in `agents/retrieval/vector.py` and is tested, but
  `build_retriever` — the function that would select it — has no caller.
  `RetrievalAgent.__init__` takes `build_index()`, which is the TF-IDF index, in
  every mode.

  This bullet previously claimed the opposite, on the reasoning that
  `build_retriever` selects the hybrid whenever `OVERTURN_RUNTIME_MODE=cloud`
  and `infra/deploy.sh` sets exactly that mode. Both halves of that are true and
  the conclusion is still false, because nothing calls the function. It is worth
  recording as a caution: a claim assembled from two correct facts about
  configuration, without checking the call site, is exactly the kind of thing
  that survives review and then fails in front of a judge who greps for it.
- Agent Registry and Agent Gateway are **our own primitives**, not the managed
  GEAP services — no public REST surface was reachable on this project.
  `docs/PLATFORM_PROBE.md` records the probe and `ARCHITECTURE.md` carries the
  mapping table.
- Memory Bank is different, and worth stating precisely rather than folding
  into the bullet above: the managed surface *was* reachable on this project
  (same probe, same `200 {}`). `core/memory.py` implements the same contract
  on Firestore anyway, by choice — a managed Memory Bank is scoped to a
  session, and what a payer-behaviour observation needs to survive is weeks
  of silence, keyed on payer/policy/reason code rather than on a member. It
  is unit-tested (`tests/test_memory.py`) and **no agent in the running
  pipeline calls it yet** — the gateway grants and the collection name exist,
  but a grant is not a call site.
- Agent Runtime is not used; the pipeline terminates at a human gate and resumes
  from Firestore, which is not what a managed runtime session is for.
- The payer endpoint is simulated. Nothing is transmitted to a real insurer.
- Demo mode compresses the response window. Disclosed in the README, in the
  video, and on screen.
