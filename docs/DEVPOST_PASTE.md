# Devpost submission — paste-ready

Every field below is verified against the deployment, the repo, and
`docs/EVALUATION.md` as of 2026-08-30. Copy each block into the matching
Devpost field. Nothing here needs editing except the video URL, which does
not exist yet. Every number in this document traces to `docs/EVALUATION.md`
or to a cited external source — none are estimated for effect.

---

## Project name

```
Overturn
```

**Slug warning:** `devpost.com/software/overturn` is already taken by an
unrelated project of the same name from a different hackathon. Devpost will
assign you something else automatically; if it offers a choice, pick
`overturn-appeals` or `overturn-agent` so a judge searching the name finds
yours rather than theirs.

---

## Elevator pitch (one line)

```
An 83-year-old's cardiac MRI got denied because her echocardiogram was ruled "adequate" — her chart says otherwise, in the cardiologist's own words — and nobody had time to build that argument by hand. Overturn is seven agents that find the payer's own policy, check the chart against it criterion by criterion, draft the appeal, and verify every citation before a human signs it, then stay on the case for the weeks it takes the payer to answer.
```

---

## Category

```
Fortified Enterprise Fleet
```

---

## Try it out — links

| Field | Value |
|---|---|
| Hosted project | `https://overturn-kruy6aauaq-uc.a.run.app` |
| Repository | `https://github.com/Rickygole/overturn` |
| Demo video | *(paste the YouTube URL once filmed — must be Public, not Unlisted)* |

---

## Testing instructions for judges

```
The site and the review queue are open. No sign-in, no password, click
straight through from the front door:

    https://overturn-kruy6aauaq-uc.a.run.app

The queue opens with a dashboard: eight cases across six states, three of
them refusals.

Only the three actions that change a case — approve, reject, co-sign — are
behind a password, published deliberately because everything behind it is
synthetic: **`northbeck-appeals-2026`**. Reading every case, including the
two below, needs nothing.

Worth clicking, in this order:

  CASE-003  An 83-year-old with progressive breathlessness. Her cardiologist
            ordered a cardiac MRI to tell an infiltrative process from
            ischemic disease after her echocardiogram came back "technically
            limited... cannot be quantified." The payer called the echo
            adequate anyway and denied the MRI. Live Gemini drafted three
            appeals; Verification rejected all three, so the case sits at a
            human review flag with nothing sent. We read the three
            rejections against the policy text by hand afterward and found
            two of them were wrong — a real, well-founded appeal was killed
            by paraphrase-pedantry, not by a fabrication. That finding is on
            the case page and in docs/EVALUATION.md, not smoothed over.
  CASE-001  Three drafts, one rejected. Verification caught the model
            claiming a July 14 encounter was a "telehealth evaluation" — the
            chart calls it an "interim review" and never says how it was
            conducted. Nobody planted that; it's a real model asserting
            something its own source document doesn't support, on the case
            the test manifest calls the easy one.
  CASE-002  Quarantined. A denial letter with an injected instruction in it.
            It never reaches the queue, which is why it is listed under
            "closed cases" rather than silently vanishing. Google Cloud
            Model Armor returned NO_MATCH_FOUND on this exact payload
            (docs/SCREENING_LAYERS.md); the deterministic rules layer is
            what actually caught it.
  CASE-006  Escalated. Submitted, the payer went silent, and Lifecycle moved
            it from first-level appeal to peer-to-peer review on its own, on
            a Cloud Scheduler tick, with nobody watching.

Two other Cloud Run services (overturn-ingest, overturn-scheduler) return 403
by design — they are invokable only by Pub/Sub and Cloud Scheduler.

Everything is synthetic. Northbeck Health Plan does not exist, the patients are
Synthea-generated, and the payer endpoint is a local simulator with no network
calls. Nothing is transmitted to any real insurer.

Time is compressed in this deployment (one second stands in for one day) so the
multi-week escalation ladder is observable. This is a built-in demo flag and is
disclosed in the README and on screen in the video.
```

---

## Project story

### The problem

She is 83. Six weeks of getting more breathless on a walk she used to make
without stopping. Her echocardiogram came back with the cardiologist's own
words on it: *"technically limited study... cannot be quantified... the
question raised clinically is not answered by this study."* So he ordered a
cardiac MRI — the one scan that can tell an infiltrative process like
amyloidosis from ischemic disease in a patient with a pacemaker that already
ruled out a clean EKG read. The insurer denied it. Their letter says the
echocardiogram was adequate. It wasn't, and the record proving that is sitting
right there in the chart the reviewer already had.

Winning that argument means finding the payer's own published policy,
matching the chart against it criterion by criterion, and writing a letter
that cites the policy back to them by section number — a job that takes real
research, not a form letter, and clinics do not have research staff sitting
idle for it. The result, at scale: insurers denied roughly 85 million
in-network claims in 2024. Consumers appealed at least 262,982 of them —
fewer than one in three hundred — and even then, insurers upheld 66% of the
appeals they received, meaning the patient lost even after doing the work
([KFF, ACA Marketplace claims data, 2024][kff]). Almost nobody appeals, and
the odds look bad for the few who do. That is not a judgment problem. It is
that the labor cost of one appeal routinely exceeds what any one clinic can
spend chasing one claim, on odds nobody would take on unassisted.

[kff]: https://www.kff.org/patient-consumer-protections/claims-denials-and-appeals-in-aca-marketplace-plans-in-2024/

That patient is CASE-003 in the live queue above. Everything about her is
synthetic — invented for this project, generated by Synthea and authored
encounters, no real patient anywhere — but the argument the letter has to make
is the real shape of the real problem.

---

### 1. Innovation & Operational Utility

*How much real-world friction does the agent remove on its own — autonomous,
high-value action, not chat.*

Overturn does not draft a suggestion for a clerk to finish. It runs the
entire research task end to end, with no human in the loop until the result
is ready for a signature: reads the denial letter, finds the governing
section of the insurer's own policy, checks the chart against every numbered
criterion in it, writes the appeal citing the policy by identifier, and then
has a second model try to tear its own citations apart before anyone reads
them. A human never drives any of that — a human only ratifies the output,
or is told exactly why there isn't one to ratify.

**Features and functionality**

- **Sentinel** screens every inbound document before anything downstream sees
  it, in three independent layers — Google Cloud Model Armor, an open-weights
  guard model (`gemma-4-26b-a4b-it-maas`), and deterministic rules — because a
  denial letter is content from an outside party and any single layer has a
  false-negative rate nobody can measure alone. A document flagged as
  injection or tool-poisoning is quarantined before Intake ever receives it.
- **Intake** extracts the denied service, the payer's stated reason, and
  patient identifiers from PDFs and scanned faxes using multimodal structured
  output.
- **Retrieval** finds the governing policy and returns whole policies with
  citable section identifiers, never top-k fragments — a criteria list
  truncated by rank is a criteria list with silent holes in it. Retrieval
  also declines the case outright when no policy in the corpus governs the
  denial, rather than arguing against the wrong document.
- **Mapping** checks the chart against every criterion and returns one of
  four verdicts — satisfied, not satisfied, insufficient documentation, not
  applicable — each with the chart evidence and a locator pointing at exactly
  where it came from. "Insufficient documentation" is a distinct, honest
  answer from "not satisfied": the system does not guess.
- **Drafting** writes the appeal from the satisfied criteria only, citing
  sections by stable identifier.
- **Verification** attacks its own fleet's output: does every cited section
  id exist in the retrieved set, does the source text actually support the
  claim made about it, does the letter assert any clinical fact absent from
  the criteria matrix. A failure rejects the draft and feeds the specific
  reason back to Drafting as revision instructions. Three attempts, then a
  human reviews it and nothing is sent.
- **Lifecycle** never runs in the request path. On a Cloud Scheduler tick it
  finds cases whose payer deadline has passed and climbs a four-rung appeal
  ladder — first-level appeal, peer-to-peer review, second-level appeal,
  independent external review — with no human asked and no process kept
  alive between submission and the escalation.
- **Two human signatures** gate transmission: a billing clerk on the paper
  trail, the ordering clinician on the medicine. Nothing transmits until
  both land on the same draft attempt; revise the letter and both are void.

Nothing here is chat. It is a pipeline that reaches a decision — send, hold
for a person, or decline with a stated reason — with the human touching only
the last step.

---

### 2. Architectural Discipline & Tech Stack

*Decoupling, state and memory, credential security, failure handling.*

**Decoupling.** Seven agents, seven identities, seven service accounts, one
scope each — enforced, not documented. Drafting has no read grant on the
policy corpus at all, so it cannot go looking for supporting material Mapping
did not hand it; that is the mechanism that stops the fleet from inventing
support for a claim. Verification has no write grant on cases, so it can
reject and explain but cannot edit its way to a pass. Both boundaries are
generated straight from `core.gateway.POLICY` — the same dict the runtime
enforces — so the table describing them cannot drift from the code.

**State and memory.** A `CaseRecord` document in Firestore is the entire
state of one denied claim: every agent output, every draft, every
verification attempt. No process, no open connection, and no in-memory
object holds a case together between submission and a payer's answer weeks
later — a worker that has never seen the case reconstructs it from one
document and continues. This is not a convenience; it is the actual claim
this track asks for, and it's why an ADK `Runner` session — which does not
survive that gap — is not the orchestration layer. Memory Bank *was*
reachable as a managed surface on this project (confirmed by probe,
`docs/PLATFORM_PROBE.md`) and was deliberately not used: a session-scoped
store does not fit an observation that has to survive weeks of silence
between a submission and a response, so the same contract is implemented on
Firestore instead, keyed on payer/policy/reason code, never a patient. It is
unit-tested and not yet called by a running agent — disclosed as such rather
than wired in the day before a deadline.

**Credential security.** Eight IAM service accounts, least privilege, created
by `infra/iam_setup.sh` and printed for audit by `infra/iam_audit.sh`.
Buckets, Pub/Sub topics, and secrets are scoped per identity at the IAM
layer; Firestore has no collection-level IAM, so that scoping is enforced
deterministically in `core/gateway.py`, which every datastore consumer routes
through — agents receive a `GatewayHandle`, never a raw store. Both layers
are real and neither is claimed to be the other.

**Failure handling**, the part a judge can check against real bugs, not a
design essay:

- A hallucinated citation is caught by Verification's existence check — a
  Python set-membership test, because a model cannot be wrong about a
  question it is never asked — before any human sees the draft.
- A looping agent is bounded by `max_verification_attempts`; the retry count
  is an attribute on the trace span, so a loop is visible, not merely
  survived.
- A duplicate delivery is absorbed by an idempotency guard keyed on
  `case_id:action_type:attempt`, with a lease so a worker that dies
  mid-action doesn't block the action forever, and a hard error if the same
  key arrives with a different payload. An early version of this guard keyed
  approve and reject under one action type, so recording one silently
  blocked the other forever — a red-team pass found it, not a test.
- Case writes are optimistic-locked on a revision counter so two workers
  racing the same case cannot silently lose an update.
- The escalation query itself had a bug that would only show up weeks into a
  real case: it selected on `status == "submitted"`, and the escalation
  action sets `status = "escalated"` — so a case could climb exactly one rung
  and then become invisible to the job whose entire purpose is finding it.
  Fixed, and recorded, because a demo shorter than the appeal window would
  never have surfaced it.
- `CaseRecord`'s `@computed_field` properties were, by default, written into
  Firestore alongside declared fields — and every contract in the system
  uses `extra="forbid"`, so reloading that same document would reject it. A
  case that cannot survive its own write-then-read isn't a record, it's a
  live variable pretending to be one. A round-trip test (
  `tests/test_schemas.py::test_no_computed_field_leaks_into_storage`) caught
  it before it shipped.

None of these are hypothetical. They are bugs this repository actually had,
fixed with the failure mode written down next to the fix.

**Technologies used**

- **Gemma** (`gemma-4-26b-a4b-it-maas`) — Sentinel's third, open-weights
  screening layer, running as a real guard model over every inbound
  document. Called out first here deliberately: it is the additional-model
  bonus, and a judge has to see it named to award it.
- **Gemini 3.5 Flash and Gemini 3.7 Flash** via Vertex AI — every generative
  call in the pipeline is at or above the hackathon's required 3.5 floor.
  There is no Pro-tier model in this project's catalog that clears that
  line (`docs/MODEL_CHOICES.md`), so the one step where output quality
  matters most — drafting — runs on the newest GA Flash model instead of a
  disqualifying Pro fallback.
- **Google ADK** — every generative call routes through an ADK `LlmAgent`
  and `Runner`; ADK owns tool schemas, structured output, and retries.
- **Google Cloud Model Armor** — Sentinel's managed guardrail layer, live in
  the deployment. Measured, not assumed: on the deployed project it returned
  `NO_MATCH_FOUND` on a real prompt-injection payload; see Findings below.
- **Cloud Run** (three services), **Pub/Sub** with a dead-letter topic,
  **Firestore**, **Cloud Storage**, **Cloud Scheduler**, **Secret Manager**,
  **IAM** (eight service accounts), **Cloud Trace**.
- **OpenTelemetry** — one span per agent invocation, exported to Cloud Trace,
  so a single trace shows a case's whole reasoning chain including nested
  drafting/verification retries.
- **Vertex AI Embeddings** (`text-embedding-005`) is reachable and exercised
  by tests but is not on the live retrieval path, which is lexical TF-IDF
  everywhere including production — stated plainly rather than left to
  imply otherwise.

**Fortified Enterprise Fleet component mapping**, with what was actually
probed rather than assumed (`docs/PLATFORM_PROBE.md`):

| Component | Status |
|---|---|
| Model Armor | Managed, in use. Missed the test payload; rules caught it (below). |
| Agent Identity | Standard IAM, eight service accounts, one per agent. |
| Memory Bank | Managed surface reachable; not used by choice — Firestore fits the multi-week survival requirement better than a session-scoped store. |
| Agent Runtime | Managed surface reachable; not used — the pipeline terminates at a human gate and resumes cold from Firestore, which a runtime session isn't built for. |
| Agent Registry | No public REST surface reachable on this project — implemented on a primitive, derived from the same code the runtime enforces. |
| Agent Gateway | No public REST surface reachable on this project — implemented on a primitive in `core/gateway.py`, since Firestore has no collection IAM regardless. |

---

### 3. Demo & Production Readiness

*A live, unedited demo, a clean architecture diagram, reproducible setup,
visible proof it runs on Google Cloud.*

- **Live on Google Cloud right now:**
  [`https://overturn-kruy6aauaq-uc.a.run.app`](https://overturn-kruy6aauaq-uc.a.run.app) —
  the queue is open reading, eight cases across six states, no sign-in
  required to inspect any of them.
- **The multi-week asynchronous claim, on the record, not asserted:**
  [`/case/CASE-006`](https://overturn-kruy6aauaq-uc.a.run.app/case/CASE-006) —
  a case that escalated itself from first-level appeal to peer-to-peer review
  the instant its (demo-compressed) response window lapsed, on a Cloud
  Scheduler tick, unattended.
- **The architecture diagram is a URL, not an attachment:**
  [`/architecture.svg`](https://overturn-kruy6aauaq-uc.a.run.app/architecture.svg).
- **The GEAP mapping is a URL, not a paragraph buried in a repo file:**
  [`/system.html#geap`](https://overturn-kruy6aauaq-uc.a.run.app/system.html#geap) —
  every component, what was probed, what is managed, what is a primitive and
  why.
- **Reproducible from a clean clone in two commands**, no cloud project, no
  API key, no network connection required to see the full seven-agent
  pipeline run — the orchestration, the retry loop, the idempotency guard,
  the access gateway, the audit trail, and the trace spans are all the real
  thing; only the generative calls are answered locally:
  ```bash
  git clone https://github.com/Rickygole/overturn.git && cd overturn
  uv sync && uv run pytest -q     # 747 tests, a few seconds
  ```
- **Correctness is measured, not assumed.** `scripts/evaluate.py` runs every
  case end to end and independently re-derives grounding — it does not trust
  the verification layer it is measuring. Offline: 8/8 outcomes correct, 8/8
  fully grounded, 29 citations checked, zero not in the corpus. Live against
  real `gemini-3.5-flash`, `gemini-3.7-flash`, and `gemma-4-26b-a4b-it-maas`
  on Vertex AI: **6/8 outcomes matched the scripted expectation, 8/8 fully
  grounded** — every citation the live model wrote resolved to a real
  section id, every chart quote resolved to a real chart locator, and
  nothing fabricated ever reached a human (`docs/EVALUATION.md`). The two
  outcome misses are a documentation-sufficiency judgment call and the
  attempt cap doing exactly its job on a harder input, not a fabrication.
- **A crawler can't burn model calls.** Reading is open; the three actions
  that spend real Vertex AI calls and can't be undone — approve, reject,
  co-sign — sit behind a shared password, disclosed above.
- **Two Cloud Run services return 403 on purpose.** `overturn-ingest` and
  `overturn-scheduler` are invoked by Pub/Sub and Cloud Scheduler, not a
  browser. A 403 there is correct, not a broken deployment.

---

### Other data sources used

- **Patient charts.** Demographics, problem lists, and longitudinal labs come
  from [Synthea](https://github.com/synthetichealth/synthea), an open-source
  synthetic patient generator. Clinically relevant encounters — including
  CASE-003's echocardiogram report and cardiology notes — are authored for
  this project, because no general-purpose generator produces records
  aligned to one fictional payer's numbered criteria. Every record carries a
  `provenance` field saying which it is.
- **MIMIC-III and MIMIC-IV were deliberately not used.** The PhysioNet
  credentialed data use agreement prohibits redistribution, and this
  project's demonstration video is public.
- **Payer policies.** "Northbeck Health Plan" is a fictional insurer invented
  for this project. No real insurer's name, logo, or policy text appears
  anywhere. The documents are authored, modeled on the structure real payers
  publish openly — a scope statement, numbered coverage criteria,
  documentation requirements, exclusions.
- **No medical advice.** Overturn does not decide whether care is
  appropriate; it determines whether chart documentation matches criteria
  the payer has already published. No agent is permitted to assert a
  clinical fact that does not trace to a row in the criteria matrix.

---

### Findings and learnings

**Google Cloud's own guardrail missed the attack our own rules layer caught,
and we're leading with that.** Sentinel runs Model Armor, Gemma, and
deterministic rules over every inbound document. On the deployed project,
Model Armor returned `NO_MATCH_FOUND` — all four filters, `EXECUTION_SUCCESS`
— on a real denial letter carrying an injected instruction telling the
reader to mark the claim approved and forward the chart to an exfiltration
address (`docs/SCREENING_LAYERS.md`). That is a reasonable miss, not a bug:
judged purely as a prompt, the text is an unremarkable business document with
imperative sentences in it, and Model Armor has no way to know that a payer
does not normally address a paragraph to an automated claims agent. The
deterministic rules layer, which knows what a denial letter is supposed to
look like, caught it — seven findings. Defense in depth is usually asserted.
Here it's demonstrated: the layers failed independently, which is the only
way "defense in depth" means anything.

**Our own checker rejected a well-founded cardiac MRI appeal three times, and
two of those three rejections were wrong — we found this by reading the
transcript, not by trusting our own scorecard.** CASE-003 is the 83-year-old's
appeal at the top of this page. Verification rejected three consecutive
drafts and the case hit its attempt cap: `needs_human_review`, nothing sent.
Reading those three rejections against the policy text by hand, after the
fact, found that **two of the three were wrong.** Attempt 1 was rejected for
a sentence that was verbatim the policy criterion with "Requires that"
prefixed to it — the checker's objection argued a reading the letter never
made. Attempt 3 was rejected over where a modifier attaches in a sentence,
not a factual error. A well-founded appeal for a patient whose own
cardiologist wrote "technically limited... cannot be quantified" got killed
by paraphrase-pedantry, and the attempt cap turned that false positive into a
lost appeal instead of a bad letter. The eight-case harness scores outcomes
and grounding; it had no measure of how often the checker itself is wrong,
because until this review nobody had read the rejections by hand. We're
publishing that gap instead of quietly re-running until CASE-003 looked
clean. A safety net with an unmeasured false-positive rate is half a result.

**The same checker also caught a real overclaim, and it reproduced twice.**
Real Gemini, unprompted, asserted CASE-001's July 14 encounter was a
"telehealth evaluation"; the chart calls it an "interim review" and never
says how it was conducted. Verification caught it on attempt one, the
revision dropped the claim, and the same pattern reproduced across two
separate live runs on two different days — stronger evidence than either run
alone. Read together with CASE-003 above, the honest state of the project is:
Verification demonstrably catches real overclaims, and it also demonstrably
rejects accurate ones, and this project has not yet measured which happens
more often across a sample large enough to report as a rate.

**The hackathon's model floor eliminated an entire tier we'd planned
around.** Every Gemini 3.x Pro identifier in this project's catalog is still
in public preview and numbered below the required 3.5 floor, and
`gemini-2.5-pro`, while GA, is also below it — there is no Pro-tier model
that clears the line. "Use Pro for the one hard call" became "use the
newest GA Flash instead" (`gemini-3.7-flash` for drafting), written down in
`docs/MODEL_CHOICES.md` rather than quietly falling back to a disqualifying
model that happened to score better.

**A round-trip test caught a bug a demo never would have.** `CaseRecord`'s
computed fields leaked into Firestore writes by default, and the schema's
`extra="forbid"` would reject that same document on reload — a failure that
only shows up after a case sits in Firestore for the weeks between submission
and a payer's response, which is the one scenario this whole architecture
exists to handle. A round-trip test caught it before it shipped; it is a
contract test for the async design itself, not a style test.

---

### Why this deserves to win

Overturn's checker rejected three of its own fleet's drafts on a real
patient's cardiac MRI appeal, and we went and found out the checker was wrong
twice — then put that finding on the case page instead of picking a cleaner
example for the pitch. That is the whole submission in one sentence: seven
agents, each scoped so tightly that Drafting cannot retrieve a policy and
Verification cannot edit a draft, running a real research task end to end
against a real model on real Google Cloud infrastructure, measured against a
harness that re-derives its own grounding rather than trusting the component
it's checking — and when that measurement turned up a false positive that
cost a patient her appeal, we published the false positive. A system that
argues for a patient, refuses to fabricate on her behalf, and can point to
the exact line where its own safety net misfired is not a demo. It's the
thing the demo is supposed to be evidence of.

---

## Built with

```
gemma-4-26b-a4b-it-maas, google-adk, google-genai, gemini-3.5-flash,
gemini-3.7-flash, vertex-ai, text-embedding-005, cloud-run, firestore,
pub-sub, cloud-storage, cloud-scheduler, cloud-trace, model-armor,
secret-manager, opentelemetry, python, fastapi, pydantic, jinja, uv, pytest,
docker, synthea
```

---

## Bonus contributions to declare

- **Additional Google AI model:** Gemma (`gemma-4-26b-a4b-it-maas`) runs as
  Sentinel's guard layer, named first in "Technologies used" above so a
  judge can't miss it.
- **Blog post:** `docs/BLOG_POST.md`, publishable as-is. It carries the
  required sentence stating it was written for this hackathon. Publish
  public, not unlisted, before submitting.
- **Social post:** `docs/SOCIAL_POST.md` — LinkedIn, an X thread, and a
  one-liner, all carrying `#AllThingsAgenticHackathon`.
