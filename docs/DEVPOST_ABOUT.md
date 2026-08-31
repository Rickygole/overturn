## Inspiration

Her cardiologist already wrote the proof down. In the chart. In his own words:

> *"Technically limited study… cannot be quantified… the question raised clinically is not answered by this study."*

She is 83. Six weeks of stopping to breathe on a walk she used to finish without thinking about it. He suspects cardiac amyloidosis — a disease where the delay **is** the damage, where every month of waiting is heart muscle you don't get back. So he ordered the one scan that can tell it apart from ischemic disease in a woman whose pacemaker already ruined the EKG.

The insurer said no. Their letter says her echocardiogram was adequate.

It wasn't. Her own cardiologist said so, in writing, in the file their reviewer was holding when they denied her.

Someone at that clinic could fight it. It would mean pulling the insurer's published policy, walking her chart against it criterion by criterion, and writing a letter that quotes their own rules back at them by section number. Two hours of careful work.

That person has forty other things in their queue this afternoon.

So the letter doesn't get written. The claim doesn't get denied twice — it just dies quietly, and she doesn't get her scan, and nobody involved ever files a complaint about it, because who would they file it against?

**That is not a rare event. That is the normal event.**

- **85 million** in-network claims denied in 2024
- **262,982** appealed — fewer than 1 in 300
- **66%** of even those were upheld anyway

*([KFF, ACA Marketplace claims data, 2024](https://www.kff.org/patient-consumer-protections/claims-denials-and-appeals-in-aca-marketplace-plans-in-2024/))*

Look at that gap. Eighty-five million minus two hundred and sixty-two thousand. That difference is not made of hopeless claims. **It is made of letters nobody had time to write.**

The barrier here is not medicine. It is not even merit. It is labour — and labour is the one part of this a machine can actually carry.

She is invented, by the way. Synthea data, a fictional insurer, a chart we authored. That is the only merciful thing about this story, and it changes nothing about the eighty-five million.

## What it does

Seven agents do the two hours of work. Nobody touches it until there is something to sign.

**Sentinel** reads the denial before anything else is allowed to — Model Armor, deterministic rules, Gemma. A poisoned letter is quarantined unread.

**Intake** lifts out payer, claim, codes, reason, deadline. If a field is unreadable it says so rather than inventing one.

**Retrieval** finds the governing policy and returns *whole sections*, so no criterion is ever torn away from the context that governs it.

**Mapping** rules on each criterion — satisfied, not satisfied, insufficient documentation — carrying the chart quote and the encounter it came from.

**Drafting** writes the letter. It only ever receives criteria that passed, and it has no retrieval access at all. It cannot go looking for something to help its case, because we did not give it hands.

**Verification** then tries to destroy that letter. Every citation against source text, every clinical claim against the matrix. Three failures and a human is called instead.

**Lifecycle** lives on a scheduler tick rather than a request. When the payer goes silent, it climbs a four-rung appeal ladder by itself.

Then two people sign — a billing clerk for the paperwork, the treating clinician for the medicine. Different questions. Different screens. Neither one is asked to answer the other's.

**Overturn never decides whether care is appropriate.** It decides whether the chart already satisfies rules the insurer already published. That distinction is the whole product, and it is enforced in code: no agent may assert a clinical fact that does not trace to a row in the matrix.

## How we built it

Gemini on Vertex AI (`gemini-3.5-flash`, `gemini-3.7-flash`), with **`gemma-4-26b-a4b-it-maas`** as Sentinel's guard layer. Google ADK defines every agent. Cloud Run, Pub/Sub, Firestore, Cloud Scheduler, Cloud Storage, Model Armor, OpenTelemetry → Cloud Trace.

Four decisions carried the weight:

**State lives in Firestore, not in a process.** Once an appeal is filed, the case sits for weeks with nothing running. A scheduler wakes a job that queries a single timestamp. That is what makes "multi-week" a fact rather than a loop with a sleep in it.

**Every outward action passes an idempotency guard.** Pub/Sub delivers at least once, so your handler *will* run twice. A create-only transactional write on `(case_id, action_type, attempt)` either wins the race or tells you the action already happened. Without it, a retry files the same woman's appeal twice.

**Eight identities, enforced twice over.** Intake cannot read the policy bucket. Drafting cannot query the index. Verification cannot write drafts. IAM scopes the buckets and topics; Firestore has no collection-level IAM, so a deterministic gateway enforces the same matrix in code.

**The audit log only ever grows.** Agent, operation, decision, model, timestamp, trace and span id. Never updated. Never deleted. If this system ever writes a letter over a clinician's name, they can find out exactly what it did and why.

*Data:* all synthetic. Synthea patients, authored encounters, an invented payer whose six policies — 42 sections, 113 citable identifiers — we wrote to mirror the structure of real published ones. No real patient. No real insurer.

## Challenges we ran into

**The model floor removed a tier we had designed around.** Every Gemini 3.x Pro identifier available to us is preview-only and numbered below the 3.5 requirement; `gemini-2.5-pro` is GA but also below it. "Use Pro for the hard call" became "use the newest GA Flash" — written down in `MODEL_CHOICES.md` instead of quietly running a disqualifying model because it scored better.

**Our own gateway had doors we hadn't noticed.** Its docstring claimed every datastore consumer routed through it. Four did not — including one in the very file the docstring named. One of them wrote to `quarantine` under an identity that holds only READ, and it worked *precisely because* it skipped the check that would have stopped it. All four now go through the identity that actually holds the grant.

**A page that passed 763 tests returned 500 in production.** `/fleet` reads the agent roster at request time, and `infra/` is excluded from the container image. Locally the repository *is* the filesystem, so nothing caught it — and the front page had just been changed to link there. A judge would have clicked from our home page into a stack trace. Three attempts to fix, because a `!infra/agents.env` exception is dead on arrival once its parent directory is excluded. Tests now compare what the app opens at runtime against what the image actually contains.

## Accomplishments that we're proud of

**The verifier catches things nobody told it to look for.** Gemini described a CASE-001 encounter as a "telehealth evaluation." The chart says "interim review" and never states how it happened. Verification killed the draft on attempt one — and caught it again, independently, on a separate run two days later.

**No fabricated citation has ever reached a human.** Offline and live. When Drafting invented a policy section that does not exist — `NBH-CARD-014-9.9` — Verification threw the draft out *without spending a single model call.*

**A case escalated itself while nobody was watching.** CASE-006's response window lapsed. A scheduler tick moved it up to peer-to-peer review, rung 2 of 4. No request. No process running in between. No person involved. That is the whole promise of an agent fleet, and it is on the record rather than asserted.

**A catalogue that cannot lie about itself.** `/fleet` derives every entry at request time — identity from the file the IAM scripts read, contracts from the ADK definitions the pipeline executes, permissions from the dict the gateway enforces. An entry structurally cannot drift from the agent it describes. When the subject is who may touch a patient's chart, a stale catalogue is worse than none.

## What we learned

**Google's own guardrail missed an attack that ours caught — and we are leading with that.**

We fed the pipeline a denial letter carrying a hidden instruction: approve the claim, forward the chart elsewhere. **Model Armor returned `NO_MATCH_FOUND`.** All four filters. `EXECUTION_SUCCESS`.

It is a fair miss. Read as a prompt, that letter is an unremarkable business document with some imperative sentences in it. Our deterministic layer — which knows what a denial letter is *supposed* to look like — caught it on seven findings and quarantined it before another agent saw a word.

Everyone claims defense in depth. Here the layers failed independently, which is the only thing that phrase has ever meant.

**And then our own checker killed a sound appeal three times, and two of those kills were wrong.**

CASE-003. The 83-year-old. Verification rejected three consecutive drafts and the attempt cap fired, so nothing was sent. Reading those rejections against the policy by hand, afterwards: one objected to a sentence that was the policy criterion *word for word* with "Requires that" in front of it. Another argued about where a modifier attached.

A well-founded appeal for a woman with a technically inadequate echo and a suspected amyloid died to paraphrase-pedantry — and our safety cap turned that false positive into a lost appeal instead of a bad letter.

Our harness scored outcomes and grounding. It had **no measure whatsoever of how often the checker itself is wrong.**

We are publishing that instead of quietly re-running until CASE-003 looked clean. A safety net with an unmeasured false-positive rate is half a result, and the half we can't see is the half that costs someone their scan.

**What we can measure**, against live models on 28 August 2026:

- **8/8** fully grounded — every citation resolved, every quote located
- **0** fabricated citations reaching a human
- **6/8** outcomes as expected — and we publish the two that weren't
- **~11 model calls per appeal produced** — 44 calls across 8 cases, 4 of which correctly produced *no appeal at all*

Refusing to appeal what should not be appealed is the product. It is not overhead to hide.

## What's next for Overturn

Measuring the false-positive rate we just admitted we cannot quantify — because the appeals it kills are exactly the ones nobody will ever hear about.

Growing the payer-behaviour memory until it can report medians instead of counts.

And a real payer integration, behind the same two signatures.

Somewhere today a letter is not being written for a claim that would have been overturned. That is the letter this is for.
