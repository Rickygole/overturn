## Inspiration

She is 83. Six weeks of getting more breathless on a walk she used to make without stopping. Her echocardiogram came back with her cardiologist's own words on it — *"technically limited study… cannot be quantified… the question raised clinically is not answered by this study."* So he ordered a cardiac MRI, the one scan that separates an infiltrative process like amyloidosis from ischemic disease in a patient whose pacemaker already rules out a clean EKG read.

The insurer denied it. Their letter says the echocardiogram was adequate. It wasn't, and the record proving that was sitting in the chart the reviewer already had.

Winning that argument means finding the payer's own published policy, matching the chart against it criterion by criterion, and writing a letter that cites the policy back by section number. That is research, not a form letter, and clinics do not keep research staff idle for it.

At scale: insurers denied roughly **85 million** in-network claims in 2024. Consumers appealed at least **262,982** of them — fewer than one in three hundred — and insurers **upheld 66%** of the appeals they did receive ([KFF, ACA Marketplace claims data, 2024](https://www.kff.org/patient-consumer-protections/claims-denials-and-appeals-in-aca-marketplace-plans-in-2024/)).

The barrier is not medicine and it is not merit. It is labour — and labour is the one part of this a machine can carry.

*(That patient is CASE-003 in the live queue. Everything about her is synthetic — Synthea plus authored encounters, an invented payer. The argument the letter has to make is the real shape of the real problem.)*

## What it does

Overturn runs the whole research task end to end, with no human in the loop until there is something to sign.

- **Sentinel** screens every inbound document *before anything else reads it* — Model Armor, deterministic rules, and Gemma. A poisoned letter is quarantined and no downstream agent ever sees the text.
- **Intake** extracts payer, claim number, denied service with CPT/ICD codes, denial reason and appeal deadline. Where a field is unreadable it records that rather than guessing.
- **Retrieval** finds the governing policy and returns **whole sections**, so a criterion is never separated from the context that governs it.
- **Mapping** goes criterion by criterion and returns *satisfied*, *not satisfied*, or *insufficient documentation* — each with the chart quote and the encounter it came from. Criteria written for a different modality are marked *not applicable* rather than argued about.
- **Drafting** writes the letter and receives **only** satisfied criteria. It has no retrieval access at all, so it cannot wander off and invent supporting material.
- **Verification** attacks the draft: every citation checked against the retrieved text, every clinical assertion checked against the criteria matrix. Failures send it back with the objection attached. Three attempts, then a human.
- **Lifecycle** runs on a Cloud Scheduler tick, not in a request. When a payer's response window lapses it climbs a four-rung appeal ladder on its own.

**Two signatures gate transmission.** A billing clerk confirms the paper trail; the ordering clinician attests to the medicine. Different people, different questions, different screens.

**The line the system never crosses:** Overturn never decides whether care is appropriate. It decides whether documentation already in the chart matches criteria the payer already published. That is a paperwork question, and it is enforced in code — no agent may assert a clinical fact that does not trace to a row in the criteria matrix.

## How we built it

**Gemini on Vertex AI** (`gemini-3.5-flash`, `gemini-3.7-flash`) with **`gemma-4-26b-a4b-it-maas`** as Sentinel's open-weights guard layer. **Google ADK** defines and executes every agent. **Cloud Run** hosts three services, **Pub/Sub** carries intake with a dead-letter topic, **Firestore** holds durable state, **Cloud Scheduler** drives the lifecycle, **Cloud Storage** receives denials, **Model Armor** screens them, and **OpenTelemetry** exports a span per invocation to **Cloud Trace**.

Four decisions did the heavy lifting:

**State lives in Firestore, not in a process.** After submission a case sits for weeks with nothing executing. A scheduler wakes a job that queries one timestamp field. That is what makes the multi-week claim real rather than a loop with a sleep in it.

**Every externally visible action passes an idempotency guard.** Pub/Sub is at-least-once, so your handler *will* run twice. A transactional create-only write on `(case_id, action_type, attempt)` either succeeds or reveals the action already happened. Without it, a retried message files the same appeal twice.

**Each agent has its own identity and its own permissions.** Eight service accounts. Intake cannot read the policy bucket; Drafting cannot query the index; Verification cannot write drafts. Firestore has no collection-level IAM, so a deterministic gateway enforces the same separation in code — and every datastore consumer routes through it.

**The audit log is append-only.** Agent, operation, decision, model, timestamp, trace and span id. Nothing is ever updated or deleted.

**Other data sources used.**

Everything is synthetic. Patients and charts come from **Synthea** plus authored encounters. **Northbeck Health Plan** is a fictional payer and its six medical policies (42 sections, 113 citable identifiers) are original documents written to mirror the structure of publicly published payer policies. No real patient data, no real payer trademarks, nothing that could violate a data-use agreement. Population statistics are cited from KFF's ACA Marketplace claims analysis.

## Challenges we ran into

**The hackathon's model floor removed a tier we had designed around.** Every Gemini 3.x Pro identifier in our catalog is preview-only and numbered below the 3.5 floor; `gemini-2.5-pro` is GA but also below it. "Use Pro for the one hard call" became "use the newest GA Flash," written down in `docs/MODEL_CHOICES.md` rather than quietly running a disqualifying model that scored better.

**A round-trip test caught a bug a demo never would have.** `CaseRecord`'s computed fields leaked into Firestore writes, and the schema's `extra="forbid"` would reject that same document on reload — a failure that only appears after a case sits for the weeks between submission and a payer's answer, which is the exact scenario the architecture exists for.

**The gateway had doors we had not noticed.** Our own docstring claimed every datastore consumer routed through the gateway. Four call sites did not — including one in the file the docstring named. One of them was writing to `quarantine` under an identity that holds only READ on it, and it succeeded *because* it skipped the check that would have refused it. All four now route through the identity that actually holds the grant.

**A page that passed every test returned 500 in production.** `/fleet` reads the agent roster at request time; `infra/` is excluded from the container image. Locally the repository *is* the filesystem, so nothing caught it. The fix took three attempts — a `!infra/agents.env` exception is dead on arrival if the parent directory is excluded, which is standard gitignore semantics and cost two failed builds. Tests now compare what the app opens at runtime against what the image actually contains.

## Accomplishments that we're proud of

**The verifier catches things we did not tell it to look for.** On a live run, Gemini described a CASE-001 encounter as a "telehealth evaluation." The chart calls it an "interim review" and never says how it was conducted. Verification caught the overclaim on attempt one, the rewrite dropped it, and the same catch reproduced on a second run two days later. Nobody wrote a rule for that sentence.

**Zero fabricated citations have ever reached a human.** Across the offline harness and the live Vertex AI run, every citation in every letter resolved to a real policy section and every chart quote to a real locator. Where Drafting invented `NBH-CARD-014-9.9`, Verification rejected the draft *without spending a model call* and the rewrite passed.

**A case escalated itself with nobody watching.** CASE-006 sat submitted, its response window lapsed, and a scheduler tick moved it to peer-to-peer review — rung 2 of 4 — with no request, no process running in between and no person involved. The track's defining claim is on the record rather than asserted, at a URL a judge can open.

**A catalogue that cannot lie about itself.** The agent registry at `/fleet` is *derived* at request time — identity from the same file the IAM scripts read, model and output contract from the ADK definitions the pipeline executes, permissions from the dict the gateway actually enforces. A registry entry structurally cannot drift from the agent it describes, which matters more than a registry existing when the subject is who may touch patient data.

**We published the results that make us look worse.** Model Armor's miss and our own checker's two wrong rejections are both on the front page of the project, next to the numbers that flatter us. That was a choice, made twice, and it is the thing we would defend hardest.

**765 tests**, including contract tests for the async design itself — a Firestore round-trip test that catches a failure which only appears after a case has been sitting for weeks.

## What we learned

**Google Cloud's own guardrail missed an attack our rules layer caught — and we are leading with that.** On a denial letter carrying an injected instruction to approve the claim and forward the chart, **Model Armor returned `NO_MATCH_FOUND`** across all four filters, `EXECUTION_SUCCESS`. It is a reasonable miss: judged purely as a prompt, the text is an unremarkable business document with imperative sentences in it. Our deterministic layer — which knows what a denial letter is *supposed* to look like — caught it with seven findings and quarantined the document. Defense in depth is usually asserted. Here the layers failed independently, which is the only way the phrase means anything.

**Our own checker rejected a well-founded appeal three times, and two of those rejections were wrong.** On CASE-003 — the 83-year-old above — Verification rejected three consecutive drafts and the attempt cap fired. Reading those rejections against the policy by hand afterwards, two were wrong: one objected to a sentence that was the policy criterion *verbatim* with "Requires that" in front of it; another argued about where a modifier attaches. A sound appeal died to paraphrase-pedantry. Our harness scores outcomes and grounding and had **no measure of how often the checker itself is wrong**. We are publishing that gap rather than re-running until the case looked clean. A safety net with an unmeasured false-positive rate is half a result.

**The same checker also caught a real overclaim, twice.** Gemini asserted a CASE-001 encounter was a "telehealth evaluation"; the chart calls it an "interim review" and never says how it was conducted. Verification caught it on attempt one and the revision dropped the claim — reproduced across two live runs on two days.

**Measured against real models, 28 August 2026:** 8/8 fully grounded (every citation resolved to a real policy section, every chart quote to a real locator), **0 fabricated citations ever reached a human**, 6/8 outcomes matched expectation — and we publish the two that did not. Cost: 44 model calls and 212,805 in / 53,224 out tokens across eight cases. Four of those eight correctly produced **no appeal at all** — a quarantine, two declines for lack of basis, one attempt cap — so the honest figure is roughly **eleven model calls per appeal produced**. Refusing to appeal what shouldn't be appealed is the product, not overhead.

## What's next for Overturn

Measuring the false-positive rate we just admitted we cannot quantify; wiring the payer-behaviour memory into a live corpus large enough to report medians rather than counts; and a real payer integration behind the same two-signature gate.
