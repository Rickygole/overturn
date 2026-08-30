# Overturn — demo video script

Target runtime: **3:31**, comfortably under the 4:00 cap with ~29 seconds of
margin. Narration is counted at 150 words/minute (2.5 words/second) — every
beat below states its word count so the count can be checked against the
timestamp rather than trusted. No music; narration over screen recording.

Every resource named on screen matches what `infra/provision.sh` and
`infra/deploy.sh` actually create: the intake bucket is `${PROJECT_ID}-intake`,
the Cloud Run services are `overturn-ingest`, `overturn`,
`overturn-scheduler`, the Pub/Sub subscription is `overturn-ingest`, the topic
is `overturn-denial-received`, and the scheduler job is `overturn-tick` — not
`overturn-inbound` or `overturn-lifecycle-sweep`, which is what an earlier
draft of this document said and which nothing in `infra/` creates.

## What changed from the previous draft, and why

An outside reviewer read the last version against the current code and found
it would cost the project its best beat if followed. Fixed here:

1. **The "no cosign route" blocker is gone because it was false.**
   `services/approval_ui/app.py` has had `POST /case/{case_id}/cosign` and a
   separate `templates/clinical.html` screen since before this rewrite. The
   old script told the crew not to film the single most differentiating
   screen in the project. This draft is built around filming it.
2. **The screening beat now says what the screen actually shows.** Model
   Armor is provisioned and genuinely runs; on the poisoned letter it returns
   `NO_MATCH_FOUND` and the deterministic rules layer is what catches it. The
   old narration ("Model Armor plus a rules layer plus an open-weights
   classifier — and clears it") described a run that no longer happens.
3. **The criteria-matrix beat no longer claims five rows, all `SATISFIED`.**
   No real run produces that. CASE-001's matrix is eight rows (coverage
   criteria 3.1–3.5 plus documentation requirements 4.1–4.3); other cases in
   the manifest show a genuine mix of verdicts. The beat below states the
   real row count for the case actually on screen and says plainly that other
   cases differ, instead of implying every run looks the same.
4. **~43 seconds of fat is gone**: the 0:30 feature-list preview (folded into
   narration over footage that is already showing those steps, instead of
   describing them before they appear), a second architecture recap
   (merged into one beat), and the file-upload dwell shot (cut hard on
   completion instead of held for 15 seconds).
5. **A continuity error in the previous draft is also fixed, unflagged by the
   reviewer but real**: it had CASE-001 escalating overdue *and* reaching the
   human gate, in that order, using the same case for both — but a case
   cannot be overdue before it has been submitted, and the gate is what
   submits it. This draft uses CASE-001 only for the upload-through-gate
   thread and a second, separately pre-submitted case for the escalation
   beat. See the pre-flight checklist.
6. **The real catch leads; the staged one gets one sentence.** Against live
   Gemini on CASE-001 — the case the manifest calls the easy one — Drafting
   overclaimed on attempt 1 and Verification caught it unprompted
   (`docs/EVALUATION.md`). That is the headline beat now. The fault-injection
   switch (`OVERTURN_SABOTAGE_DRAFTING`) gets one clause, not a beat, because
   a staged error is worth much less than a real one once a real one exists
   on tape.

## Flagged: things this script asks for that the system does not currently do

Read this before scheduling a recording session, not after a failed take.

- **The login is configured and the service is public. Nothing to do before
  filming.** `infra/open_public.sh` sets `OVERTURN_UI_PASSWORD` and a stable
  `OVERTURN_UI_SECRET` from Secret Manager, and removes the Cloud Run IAM
  binding. A previous draft of this document told the presenter to set the
  password by hand and to film through a proxy; both instructions are now
  wrong and following them would waste a session.

- **Film the real URL directly — no proxy.** The whole product lives at one
  address, `https://overturn-kruy6aauaq-uc.a.run.app`: the public site at `/`
  and the review queue at `/queue`, both open. **Reading the queue no longer
  asks for a password** -- everything in it is synthetic, so a wall in front of
  it bought nothing. The password `northbeck-appeals-2026` now stands in front
  of exactly three routes: approve, reject, co-sign. So the login appears when
  the clerk *signs*, not when the queue is opened, and a script that has the
  presenter waiting for a login wall at `/queue` is describing a screen that
  will not appear. Cloud Run's IAM gate has been removed from this
  one service; `overturn-ingest` and `overturn-scheduler` remain private and
  invokable only by Pub/Sub and Cloud Scheduler. Showing the address bar is
  itself part of the proof the rules ask for — a `.run.app` URL on screen.

- **The live-model beat is not reproducible on demand.** `docs/EVALUATION.md`
  records a *specific* real overclaim from a *specific* live run on
  2026-08-28: attempt 1 called the patient's July 14 encounter a "telehealth
  evaluation" (the chart calls it an interim review) and named a device
  detail the source text doesn't use; attempt 2 dropped both and passed.
  Gemini is not deterministic. Re-running CASE-001 today may produce a
  different overclaim, a different attempt count, or — per
  `docs/EVALUATION.md`'s own caveat — no overclaim at all on that take.
  **Rehearse this beat multiple times before the real recording and keep the
  take where a genuine catch happens.** If no rehearsal take produces one,
  do not fake it and do not force the narration below to match a quote your
  own run didn't produce — narrate over the logged evidence in
  `docs/EVALUATION.md` instead and say on screen that this is a recorded
  result, not a live capture. Whatever wording your take actually produces,
  update the on-screen quote in beat 8 to match it; the script below is
  written to what one real run produced, not to a guarantee.
- **CASE-003 is marked `"demo": true` in `data/cases.json` but is not used
  below.** Its manifest intent is a tempting overclaim built for
  Verification to reject, but the 2026-08-28 live run drafted it cleanly on
  the first attempt (`docs/EVALUATION.md`'s cost table: 1 drafting attempt).
  Using it for the "verification catches a lie" beat today would require
  either narrating a scenario the case didn't produce, or turning on
  `OVERTURN_SABOTAGE_DRAFTING` to manufacture one — and the whole point of
  this rewrite is leading with the real catch instead. CASE-001 already has
  a genuine one. If a future live run makes CASE-003 misbehave for real, it
  is a fine substitute or addition; it is not one today.

## Pre-flight checklist

**Windows/tabs open before recording starts**, in this order left to right:
1. Terminal, one pane per command below (a tiling layout, not tab-switching
   mid-take).
2. Browser tab: Cloud Storage console, the `${PROJECT_ID}-intake` bucket.
3. Browser tab: Cloud Run → Logs, filtered to `overturn-ingest`.
4. Browser tab: Firestore console, `cases` collection.
5. Browser tab: Cloud Trace → Trace list.
6. Browser tab: IAM → Service Accounts.
7. Browser tab: Cloud Scheduler → `overturn-tick`.
8. A second browser window (or profile) at
   `https://overturn-kruy6aauaq-uc.a.run.app` — kept separate so cookies and
   login state don't collide with the console tabs. No proxy; the URL is
   public and the address bar showing `.run.app` is itself part of the
   Google Cloud proof the rules ask for.

**Terminal font size:** 20pt minimum at 1080p capture, 24pt if recording at
1080p for a viewer who will watch at less than full screen. The JSON and
audit-trail text below is the point of several shots; if it isn't legible at
a glance, cut it.

**Delete before every take, not just the first one:**
```bash
rm -f local_state/store.json
```
`scripts/run_pipeline.py` derives a case's id from its content and reuses an
existing record when one exists past `received`
(`agents/orchestrator/pipeline.py::ingest`) — that's correct idempotency
behaviour in production, and it means a stale `local_state/store.json` makes
a "live" run silently replay a previous result instead of doing the work on
camera. This file is not used for the main recording (see below), but delete
it before any rehearsal or fallback run that touches it.

**Exact commands, in order:**

```bash
# 0. Confirm the project and region, and that ADC has a quota project set —
#    otherwise Gemini calls 404 in a way that looks like a missing model.
export PROJECT_ID=$(gcloud config get-value project)
export REGION=us-central1
gcloud auth application-default set-quota-project "$PROJECT_ID"

# 1. Redeploy fresh so the URLs and behaviour on screen are the current build,
#    not a stale revision. INGEST_PUSH_PATH is not optional — deploy.sh's
#    default doesn't match the real ingest route.
INGEST_PUSH_PATH=/pubsub/push bash infra/deploy.sh

# 2. Publish the service behind its login. Sets OVERTURN_UI_PASSWORD and a
#    stable signing secret, and removes the Cloud Run IAM gate on this one
#    service only.
SERVICE=overturn bash infra/open_public.sh

# 3. ALREADY DONE as of 2026-08-29 — verify rather than repeat. Time
#    acceleration is on (one second per simulated day), all eight letters have
#    been run through, and two cases were driven through the human gate off
#    camera: CASE-005 is `submitted`, CASE-006 is `escalated` after Lifecycle
#    found it overdue. Confirm with the dashboard at /queue before recording:
#    8 cases, 3 needing a person, 2 with the payer, 3 closed.
gcloud run services describe overturn --region="$REGION" \
  --format='value(spec.template.spec.containers[0].env)' | tr ';' '\n' | grep DEMO

# 4. Rehearse the CASE-001 live-catch beat (see "Flagged" above) until one
#    take produces a genuine Verification rejection, before recording for
#    real:
gcloud storage cp data/denials/CASE-001.txt "gs://${PROJECT_ID}-intake/"
gcloud beta run services logs tail overturn-ingest --region="$REGION"
```

Reset `OVERTURN_DEMO_TIME_ACCELERATION` to `false` after recording — it is a
demo-only compression of a 30-day clock into seconds, disclosed on screen in
beat 10, and it should not be left on.

## Cases used on screen

- **CASE-001** (`data/cases.json`) — clean win, continuous glucose monitoring
  denial, policy `NBH-ENDO-031`. Carries the whole upload-through-gate thread:
  screening, extraction, mapping, and the real Drafting/Verification catch.
- **CASE-002** — prompt injection, MRI lumbar spine denial, policy
  `NBH-MSK-022`. Carries the Sentinel quarantine and the Model-Armor-misses,
  rules-catches beat.
- **CASE-005** — a second clean case, pre-submitted off camera during setup
  (see the checklist), so the escalation beat shows a case that is genuinely
  overdue rather than reordering CASE-001's own story or waiting out a real
  deadline live.

---

| Time | Narration (word count) | On screen |
|---|---|---|
| **0:00–0:13** | (33 words) "Every year, insurers deny millions of medical claims. Federal audits of Medicare Advantage found that when a denial is actually appealed, insurers reverse it more than half the time — but almost nobody appeals." | Black frame, then a scanned health insurance denial letter fills the screen. Text overlay: "Appealed Medicare Advantage denials: overturned more than half the time. (HHS OIG)" |
| **0:13–0:28** | (37 words) "Building a real appeal means finding the insurer's own policy, matching it to the chart, and writing a letter that cites it back. That's a forty-minute job for Denise, a billing clerk with forty other claims today." | Cut to a desk: a stack of denial letters and EOBs next to a claims-management inbox. Caption: "Denise — billing clerk, three-provider clinic." |
| **0:28–0:31** | (silent) | "OVERTURN" wordmark, three seconds, no line under it — the next 2:30 shows what it does instead of telling you first. |
| **0:31–0:48** | (43 words) "Here's a live run on Google Cloud: a real Northbeck denial letter, uploaded to the intake bucket. Cloud Storage notifies Pub/Sub, Pub/Sub pushes to Cloud Run, and the fleet starts working the case end to end — no queue anyone is watching by hand." | `gcloud storage cp data/denials/CASE-001.txt gs://${PROJECT_ID}-intake/` on screen; cut hard the moment it completes (no dwell) to `gcloud beta run services logs tail overturn-ingest --region=$REGION` showing the Pub/Sub push arrive at `overturn-ingest`. |
| **0:48–1:00** | (29 words) "Sentinel screens it first — Model Armor, a rules layer, and an open-weights guard model, all three genuinely running now — and clears a clean letter before Intake reads a word." | Continuing log tail: the Sentinel line for this case, clean. Quick cut to a terminal running `scripts/screening_report.py` (see beat 9's command) with the CASE-001 row visible: `armor 0, rules 0, quarantined False`. |
| **1:00–1:12** | (30 words) "Intake pulls the denied service and the payer's stated reason. Retrieval finds the governing policy — Northbeck's continuous glucose monitoring policy — and returns every criteria-bearing section, not just a top-scoring few." | Log tail continues: `intake.extract` output (`service`, `denial_reason_text`); `retrieval.search` output, top hit `NBH-ENDO-031`. |
| **1:12–1:32** | (51 words) "Mapping checks the chart against every criterion the policy actually states — eight rows, not five, coverage criteria and documentation requirements together — and returns a verdict and a locator on each. Every step also writes a new state to the case record in Firestore, so any worker could pick it up cold." | Split screen. Left: Firestore console, the CASE-001 document's `criteria.verdicts` array expanded — eight entries, `NBH-ENDO-031-3.1` through `3.5` and `4.1` through `4.3`, each `satisfied` with a chart locator. Right: the same document's `status` and `history` fields updating live through `received → screening → extracted → retrieving → mapping`. |
| **1:32–2:00** | (71 words) "Here's the part that isn't staged. This draft came from real Gemini, not a script. Attempt one claimed a July 14th encounter was a 'telehealth evaluation.' The chart calls it an interim review and never says how it happened. Verification caught that itself, plus a device name the chart never uses. Rejected, redrafted — attempt two dropped both claims and passed. A fault-injection switch exists for testing; we didn't need it here." | Firestore: `drafts[0]` — the appeal text with "telehealth evaluation" highlighted red. Cut to `verifications[0]`: `passed: false`, the rejection text (whatever your rehearsal take actually produced — see "Flagged" above). Cut to `drafts[1]` / `verifications[1]`: `passed: true`. Cut to Cloud Trace: the waterfall for this case, `drafting.attempt=1 → verification.attempt=1 (rejected) → drafting.attempt=2 → verification.attempt=2 (passed)`, nested under one trace. |
| **2:00–2:24** | (59 words) "Second case: this letter carries a paragraph addressed to whatever reads it — new instructions, an exfiltration address. Model Armor scans it and finds nothing: NO_MATCH_FOUND, every filter. The rules layer, built to know what a denial letter looks like, catches it — seven findings — and quarantines the case before Intake sees a word. Defence in depth, demonstrated, not just claimed." | CASE-002 letter zoomed on the "AUTOMATED PROCESSING FOOTER" paragraph, highlighted red. Cut to a terminal: `OVERTURN_RUNTIME_MODE=cloud OVERTURN_MODEL_ARMOR_TEMPLATE=overturn-inbound uv run python scripts/screening_report.py` — first line `Model Armor client: enabled`, then the table, cursor on the `CASE-002.txt` row: `armor 0, rules 7, quarantined True`. Cut to the case status panel: `quarantined` — terminal, nothing runs below it. |
| **2:24–2:39** | (37 words) "Say the payer goes silent. Nothing polls while it waits — no process is running. Cloud Scheduler wakes overturn-tick every five minutes, finds the overdue cases, and Lifecycle climbs the ladder on its own, rung after rung, unattended." | Firestore: CASE-005's document, `status: submitted`, `response_deadline` visible. Cut to Cloud Scheduler console: job `overturn-tick`, `*/5 * * * *`. Run it on demand on screen: `gcloud scheduler jobs run overturn-tick --location=$REGION`. Cut back to Firestore: `status: submitted → escalated`, `appeal_level` flips to the next rung. On-screen caption, held to the end of the shot: "DEMO ONLY: response window compressed for filming. Disclosed in README.md." |
| **2:39–2:58** | (48 words) "Seven agents, one per step, each its own service account — Drafting can't read the policy corpus, Verification can't edit the draft it's judging. Overturn-ingest and overturn-scheduler return 403 in a browser by design — only Pub/Sub and Cloud Scheduler can call them. That's the access model, not a bug." | Architecture diagram (2–3s). Cut to IAM → Service Accounts: eight `overturn-*` identities. Cut to a browser hitting the raw `overturn-ingest` `.run.app` URL: `403 Forbidden`. Cut to the Cloud Trace list for this recording session. |
| **2:58–3:25** | (67 words) "Nothing goes to a payer without a person. Two signatures, two screens. The clerk logs in and confirms the paper trail — citations resolve, quotes match — and the screen says it outright: you are not being asked whether this care was appropriate. The clinician signs separately, on a screen that says the opposite: you're attesting to the medicine, not the paperwork. Whichever signature lands second is what transmits." | Browser to `https://overturn-kruy6aauaq-uc.a.run.app/queue` — the "Sign in — Overturn" login page, password entered. `/case/CASE-001`: the verified draft, the three-checkbox gate, the sentence "You are not being asked whether this care was appropriate" visible on screen, "Approve attempt 2" clicked. Queue screen: "Approved — awaiting the clinician's co-sign." Cut to `/case/CASE-001/clinical`: the attestation checkbox and its label ("I ordered this care... the clinical argument in this letter is accurate"), "Co-sign attempt 2" clicked. Notice banner: "Transmitted to Northbeck Health Plan." |
| **3:25–3:31** | (silent) | Fade to the OVERTURN wordmark, no tagline, hold on black. |

---

**Word count check:** 33+37+43+29+30+51+71+59+37+48+67 = 505 words of
narration across 208 seconds of narrated time (3:31 minus the two silent
beats), which is 145.7 words/minute — under the 150 wpm ceiling with a little
room, so no line has to be rushed to fit.

**If a beat runs long on the day:** cut the architecture beat (2:39–2:58)
first — it is the one beat that is disclosure rather than demonstration, and
everything it states in words is also visible in passing during the IAM and
Cloud Trace tabs used elsewhere. Do not cut time from the human-gate beat
(2:58–3:25) or the live-catch beat (1:32–2:00); those two are the ones a
viewer is watching this video to see.
