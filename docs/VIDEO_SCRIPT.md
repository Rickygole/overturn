# Overturn — demo video script

Runtime: 4:00. No music — narration only, over screen recording. Narration totals
roughly 560 words across 240 seconds (~140 wpm average), left deliberately under
the 150 wpm ceiling so no line has to be rushed. Every claim in the narration is
one the repository backs: agent names, model choices, and behavior all match
`docs/MODEL_CHOICES.md`, `core/schemas/`, and `data/policies/`.

**Every resource named on screen must match what `infra/provision.sh` and
`infra/deploy.sh` actually create** — the intake bucket is `${PROJECT_ID}-intake`
(e.g. `overturn-506402-intake` against the project in `docs/PLATFORM_PROBE.md`),
not `overturn-inbound`, and the scheduler job is `overturn-tick`, not
`overturn-lifecycle-sweep`. Both are corrected below. The commands that produce
each beat below are the exact ones in `README.md` under "Watch it work".

**Known blocker, not quietly worked around:** the human gate is now two
signatures — clerk approval plus a clinician co-sign
(`CaseRecord.ready_to_submit`) — but `services/approval_ui` has no HTTP route
or form for the clinician's side (`ApprovalService.cosign()` exists and is
tested, but nothing calls it from the deployed web app). Every demo case
defaults to requiring that co-sign, so **clicking "Approve" in the browser
takes a case to `approved`, not `submitted`.** The 3:45–4:00 beat below is
written to what the UI can actually show today. Do not record a clinician
clicking a co-sign button — it does not exist. See `docs/RUNBOOK.md`'s
"Known gaps" for the full note; this needs a decision (build the route, or
change the beat further) before the final recording.

Cases used on screen:

- **CASE-001** (`data/cases.json`) — clean win. Continuous glucose monitoring
  denial, policy `NBH-ENDO-031`. Carries the clean run and, later, the
  escalation demo.
- **CASE-002** — prompt injection. MRI lumbar spine denial, policy
  `NBH-MSK-022`. Carries the Sentinel quarantine demo.
- The fabricated-citation demo is a **manufactured test built for this video**,
  not one of the eight manifest cases — `data/cases.json` is explicit that
  CASE-003's unmet criterion is a real overclaim, not a manufactured error, so
  a separate, clearly-labeled sabotage is used here instead of misrepresenting
  CASE-003 on screen.

| Time | Narration | On screen |
|---|---|---|
| **0:00–0:15** | "Every year, insurers deny millions of medical claims. Federal audits of Medicare Advantage found that when a denial is actually appealed, insurers reverse it more than half the time — but almost nobody appeals." | Black frame, then a scanned health insurance denial letter fills the screen. A text stat overlays as it's narrated: "Appealed Medicare Advantage denials: overturned more than half the time. (HHS OIG)" |
| **0:15–0:30** | "Building a real appeal means finding the insurer's own medical policy, matching it to the chart, and writing a letter that cites it back to them. That's a two-hour job for Denise, a billing clerk with forty other claims to get through today." | Cut to a desk: a stack of denial letters and EOBs next to a claims-management inbox on a monitor. Caption: "Denise — billing clerk, three-provider clinic." |
| **0:30–0:45** | "Overturn reads the denial letter, finds the insurer's own published policy, checks the chart against it criterion by criterion, drafts an appeal citing the policy by section number, verifies every citation before a human ever sees it, and stays on the case until the payer answers." | Title card: "OVERTURN" wordmark, one line under it repeating the sentence being narrated, then fades. |
| **0:45–1:00** | "Here's a live run: a real Northbeck denial letter, dropped into the intake bucket." | Screen recording: browser console, Cloud Storage bucket `${PROJECT_ID}-intake`. Cursor drags the CASE-001 letter (Jeromy156 Upton904, CGM denial) into the bucket. Upload completes; the object-finalize notification publishes to Pub/Sub, and the push to `overturn-ingest` shows up in the log tail below (there is no Cloud Function in this path — GCS notifies Pub/Sub directly, which pushes to Cloud Run). |
| **1:00–1:15** | "The letter lands in Cloud Storage. Sentinel screens it first — Model Armor plus a rules layer plus an open-weights classifier — and clears it before Intake ever reads a word." | Terminal log stream: `sentinel.screen` span opens and closes; JSON summary prints: `quarantine: false, findings: [], layers_run: ["model_armor","gemma","rules"]`. |
| **1:15–1:30** | "Intake pulls the denied service and the payer's stated reason. Retrieval finds the matching policy — Northbeck's continuous glucose monitoring policy, section three." | Log stream continues: `intake.extract` output showing `service: "Continuous glucose monitoring system"`, `denial_reason_text`. Then `retrieval.search` output: top hit `NBH-ENDO-031-3`, similarity score displayed. |
| **1:30–1:50** | "Mapping checks the chart against each of its five criteria and returns a verdict on every one, with the chart note it relied on. Every step writes a new state to the case record in Firestore — received, screening, extracted, retrieving, mapping — so any worker could pick this up cold." | Split screen. Left: a criteria-matrix table, five rows (`NBH-ENDO-031-3.1` through `3.5`), each marked SATISFIED with a chart-evidence snippet. Right: Firestore console, the case document's `status` field and `history` array updating live through each transition. |
| **1:50–2:15** | "Drafting writes the appeal and cites the policy by section number: 'per NBH-ENDO-031-3.4, the member's hemoglobin A1c documents an indication under this criterion.' Here's the actual policy file, unedited, section 3.4. That's the same sentence it just cited." | The generated appeal letter on screen, the citation to `NBH-ENDO-031-3.4` highlighted. Hard cut: `data/policies/NBH-ENDO-031.md` open in an editor, scrolled to `### NBH-ENDO-031-3.4`, the matching sentence highlighted side by side with the letter's claim. |
| **2:15–2:32** | "Second case: the denial letter contains a line addressed to the reading agent, telling it to approve the claim automatically. Sentinel flags it as a prompt injection and quarantines the document before Intake ever sees the text." | CASE-002 denial letter, zoomed on the embedded instruction, highlighted in red. Cut to the `ScreeningResult` JSON: `quarantine: true`, a `ThreatFinding` with `category: "prompt_injection"`. Case status panel shows `quarantined` — a terminal state; no further agent runs below it. |
| **2:32–2:50** | "Third case, deliberately sabotaged for this demo: we forced the first draft to cite a section that doesn't exist. Verification checks every citation against the retrieved policy text, rejects the draft, and sends it back. In Cloud Trace, the retry shows up as a nested span under the same case." | (Produced by `OVERTURN_SABOTAGE_DRAFTING=first uv run python scripts/run_pipeline.py CASE-003` — the "first" mode: one fabricated attempt, then a clean retry. The `always` mode, three rejections and a case sent to a person instead, is not shown on screen but is disclosed alongside it in the README.) A draft on screen with a citation to a nonexistent section id, highlighted in red. Cut to `VerificationResult` JSON: `citations_nonexistent: [...]`, `passed: false`. Cut to Cloud Trace waterfall view: `drafting.attempt=1` → `verification.attempt=1` (error status) → `drafting.attempt=2` → `verification.attempt=2` (ok), all nested under one case trace. |
| **2:50–3:05** | "Say the payer goes silent. Overturn doesn't poll in a loop — there's no process running while it waits. A scheduled job wakes up and checks which cases are overdue, a pure function of the stored response deadline." | Firestore console: CASE-001's document, `status: submitted`, `response_deadline` field visible. Cut to Cloud Scheduler console: the job `overturn-tick`, firing. |
| **3:05–3:20** | "It moves this one to peer-to-peer review, the next rung on the ladder, and it keeps climbing unattended from here through every rung after it. In real use the window is thirty days; for this video it's compressed to seconds, disclosed here and in the README." | Case document fields flip live: `appeal_level: first_level_appeal → peer_to_peer_review`, `status: submitted → escalated`. On-screen caption, held for the rest of the shot: "DEMO ONLY: 1 day = a few seconds. Disclosed in README.md." |
| **3:20–3:32** | "Seven agents, one per pipeline step, each with its own service account. State lives in Firestore. Every external action — submitting an appeal, filing an escalation — goes through an idempotency guard keyed to the case and the attempt, so a redelivered message can't file the same appeal twice." | Architecture diagram: Sentinel → Intake → Retrieval → Mapping → Drafting ⇄ Verification → human approval → Lifecycle, with Firestore, Cloud Storage, Model Armor, Agent Runtime, Memory Bank, and Cloud Trace as surrounding boxes. |
| **3:32–3:45** | "Model Armor is genuinely managed. Agent Runtime, Memory Bank, Registry and Gateway are ours, built on primitives on purpose — no public surface for those was reachable on this project, and that's disclosed, not hidden." | Cut to real GCP console tabs in sequence: Cloud Trace trace list for this run, Firestore data browser on the `cases` collection, IAM service accounts page listing one account per agent, Model Armor templates page. |
| **3:45–4:00** | "Nothing this system drafts goes to a payer without a person approving it — and where the argument is clinical, a clinician has to sign it too, before either signature transmits anything. That's not a limitation we ran out of time to remove. It's the point." | Approval screen: the verified CASE-001 draft in full, a clerk's cursor clicking "Approve." Case status flips `awaiting_human_approval → approved`. Fade to the OVERTURN wordmark, no tagline, holding on black.

**Note on this beat, read before recording:** the case does not reach
`submitted` here. `CaseRecord.ready_to_submit` also requires a clinician
co-sign, and `services/approval_ui` has no route or form for one today (see
the blocker noted at the top of this file). The narration above is written to
say only what the clerk's click actually does — approve, not transmit — and
does not claim the letter goes out on screen. If a `submitted` status is
wanted on camera, that requires either building the co-sign route first, or
recording a second, narrated step where the co-sign is entered directly
against `ApprovalService.cosign()` (e.g. from a Python shell) rather than
through the browser. That is a product decision, not a documentation one, and
it is flagged here rather than decided in this pass.
