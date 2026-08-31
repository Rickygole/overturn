## Inspiration

Her cardiologist already wrote the proof down. In her chart. In his own words:

> *"Technically limited study… cannot be quantified… the question raised clinically is not answered by this study."*

She is 83. He suspects cardiac amyloidosis — where the delay **is** the damage — so he ordered the one scan that can confirm it.

The insurer said no. Their letter says her echocardiogram was adequate. It wasn't, and her cardiologist said so in the file their reviewer was holding.

Someone at that clinic could fight it: pull the insurer's published policy, walk her chart against it criterion by criterion, quote their own rules back by section number. Two hours of careful work — and that person has forty other things in their queue this afternoon.

So the letter never gets written. The claim isn't denied twice; it just dies quietly, and she doesn't get her scan.

**That is the normal event, not a rare one:**

- **85 million** in-network claims denied in 2024
- **262,982** appealed — fewer than 1 in 300
- **66%** of even those, upheld anyway

*([KFF, 2024](https://www.kff.org/patient-consumer-protections/claims-denials-and-appeals-in-aca-marketplace-plans-in-2024/))*

Eighty-five million minus two hundred sixty-two thousand. **That gap is letters nobody had time to write.**

The barrier isn't medicine, and it isn't merit. It's labour — the one part a machine can carry.

*(She's invented — Synthea data, a fictional insurer. That's the only merciful thing about this story, and it changes nothing about the 85 million.)*

## What it does

Seven agents do the two hours of work. Nobody touches it until there's something to sign.

**Sentinel** screens the denial before anything else reads it — Model Armor, deterministic rules, Gemma — and quarantines poisoned documents unread. **Intake** extracts the fields, saying so when one is unreadable rather than inventing it. **Retrieval** returns *whole policy sections*, so no criterion is torn from its context. **Mapping** rules on each criterion, carrying the chart quote and the encounter behind it. **Drafting** writes the letter — it receives only criteria that passed and has no retrieval access at all, so it cannot go hunting for support. **Verification** then tries to destroy that letter: every citation against source text, every claim against the matrix. Three failures and a human is called instead. **Lifecycle** runs on a scheduler tick, climbing a four-rung appeal ladder alone when the payer goes silent.

Then two people sign — a billing clerk for the paperwork, the treating clinician for the medicine.

**Overturn never decides whether care is appropriate.** It decides whether the chart already satisfies rules the insurer already published. That's enforced in code: no agent may assert a clinical fact that doesn't trace to a row in the matrix.

## How we built it

Gemini on Vertex AI (`gemini-3.5-flash`, `gemini-3.7-flash`) with **`gemma-4-26b-a4b-it-maas`** as Sentinel's guard layer. Google ADK, Cloud Run, Pub/Sub, Firestore, Cloud Scheduler, Cloud Storage, Model Armor, OpenTelemetry → Cloud Trace.

**State lives in Firestore, not in a process.** After filing, a case sits for weeks with nothing running; a scheduler wakes a job that queries one timestamp. That's what makes "multi-week" a fact rather than a loop with a sleep in it.

**Every outward action passes an idempotency guard.** Pub/Sub delivers at least once — without a create-only write on `(case_id, action_type, attempt)`, a retry files the same woman's appeal twice.

**Eight identities, enforced twice.** Intake can't read the policy bucket; Verification can't write drafts. IAM scopes buckets and topics; Firestore has no collection-level IAM, so a deterministic gateway enforces the same matrix in code.

*All data is synthetic:* Synthea patients, authored encounters, an invented payer whose six policies (42 sections, 113 citable identifiers) mirror the structure of real published ones.

## Challenges we ran into

**Our own gateway had doors we hadn't noticed.** Its docstring claimed every consumer routed through it. Four didn't — including one in the very file the docstring named. One wrote to `quarantine` under an identity holding only READ, and it worked *precisely because* it skipped the check that would have stopped it.

**A page that passed 763 tests returned 500 in production.** `/fleet` reads the agent roster at request time; `infra/` is excluded from the container image. Locally the repo *is* the filesystem, so nothing caught it — and the front page had just been changed to link there. Tests now compare what the app opens at runtime against what the image contains.

## Accomplishments that we're proud of

**The verifier catches what nobody told it to look for.** Gemini called a CASE-001 encounter a "telehealth evaluation"; the chart says "interim review" and never states how it happened. Verification killed it on attempt one — and caught it again, independently, two days later.

**No fabricated citation has ever reached a human.** When Drafting invented a section that doesn't exist — `NBH-CARD-014-9.9` — Verification threw the draft out *without spending a single model call.*

**A case escalated itself while nobody watched.** CASE-006's window lapsed, a scheduler tick moved it to peer-to-peer review, rung 2 of 4. No request, no process running in between, no person involved.

## What we learned

**Google's own guardrail missed an attack that ours caught — and we're leading with that.**

On a denial letter carrying a hidden instruction to approve the claim and forward the chart, **Model Armor returned `NO_MATCH_FOUND`.** All four filters. It's a fair miss — as a prompt, that letter is an unremarkable business document. Our deterministic layer, which knows what a denial letter *should* look like, caught it on seven findings and quarantined it.

Everyone claims defense in depth. Here the layers failed independently, which is the only thing that phrase has ever meant.

**Then our own checker killed a sound appeal three times, and two of those kills were wrong.**

CASE-003 — the 83-year-old. Reading the rejections against the policy by hand afterwards: one objected to a sentence that was the criterion *word for word* with "Requires that" in front of it. Her appeal died to paraphrase-pedantry, and our safety cap turned a false positive into a lost appeal.

Our harness scored outcomes and grounding. It had **no measure of how often the checker itself is wrong.** We're publishing that instead of quietly re-running until CASE-003 looked clean — because a safety net with an unmeasured false-positive rate is half a result, and the half we can't see is the half that costs someone their scan.

**What we can measure**, against live models, 28 Aug 2026:

- **8/8** fully grounded · **0** fabricated citations reaching a human
- **6/8** outcomes as expected — and we publish the two that weren't
- **~11 model calls per appeal produced** (44 calls, 8 cases, 4 of which correctly produced *no appeal at all*)

Refusing to appeal what shouldn't be appealed is the product, not overhead.

## What's next for Overturn

Measuring the false-positive rate we just admitted we can't quantify — because the appeals it wrongly kills are the ones nobody ever hears about.

Somewhere today a letter isn't being written for a claim that would have been overturned. That's the letter this is for.
