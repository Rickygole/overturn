## Inspiration

Her cardiologist already wrote the proof down. It's in her chart, in his words:

> *"Technically limited study... cannot be quantified... the question raised clinically is not answered by this study."*

She's 83. He thinks it might be cardiac amyloidosis, where waiting is the damage, so he ordered the one scan that can actually confirm it.

The insurer said no. Their letter says her echocardiogram was adequate. It wasn't, and her own cardiologist had said so in the file their reviewer was looking at.

Someone at the clinic could fight this. They'd have to pull up the insurer's published policy, go through her chart criterion by criterion, and write a letter quoting the insurer's own rules back at them by section number. That's about two hours of careful work, and the person who'd do it has forty other things waiting this afternoon.

So the letter doesn't get written. The claim isn't denied a second time. It just goes away, and she doesn't get her scan.

This is the normal outcome, not a rare one:

* **85 million** in-network claims were denied in 2024
* **262,982** were appealed, fewer than 1 in 300
* **66%** of even those appeals were upheld anyway

*([KFF, 2024](https://www.kff.org/patient-consumer-protections/claims-denials-and-appeals-in-aca-marketplace-plans-in-2024/))*

Do the subtraction. Eighty-five million minus two hundred sixty-two thousand. **That gap is letters nobody had time to write.**

The problem isn't medicine and it isn't merit. It's labour, which happens to be the part a machine can take over.

*(She's invented. Synthea data, a fictional insurer. That's the only merciful thing about the story, and it doesn't change the 85 million.)*

## What it does

Seven agents do the two hours of work. Nobody has to touch it until there's something to sign.

**Sentinel** reads the denial before anything else is allowed to, using Model Armor, deterministic rules, and Gemma. Poisoned documents get quarantined unread. **Intake** pulls out the fields, and when one isn't readable it says so instead of guessing. **Retrieval** returns whole policy sections, so a criterion never gets separated from the context that governs it. **Mapping** rules on each criterion and carries the chart quote and encounter behind it. **Drafting** writes the letter, but it only receives criteria that passed and has no retrieval access at all, so it can't go looking for something to help its case. **Verification** then tries to tear that letter apart: every citation checked against source text, every clinical claim checked against the matrix. Three failures and it stops and calls a human. **Lifecycle** runs on a scheduler tick, and climbs a four rung appeal ladder by itself when the payer goes quiet.

Then two people sign. A billing clerk handles the paperwork question, the treating clinician handles the medical one.

**Overturn never decides whether care is appropriate.** It only decides whether the chart already satisfies rules the insurer already published. That's enforced in the code: no agent can assert a clinical fact that doesn't trace back to a row in the matrix.

## How we built it

Gemini on Vertex AI (`gemini-3.5-flash` and `gemini-3.7-flash`), with **`gemma-4-26b-a4b-it-maas`** as Sentinel's guard layer. Google ADK, Cloud Run, Pub/Sub, Firestore, Cloud Scheduler, Cloud Storage, Model Armor, and OpenTelemetry into Cloud Trace.

**State lives in Firestore, not in a running process.** Once an appeal is filed the case sits for weeks with nothing executing. A scheduler wakes up a job that queries one timestamp field. That's the difference between real multi-week operation and a loop with a sleep in it.

**Every outward action goes through an idempotency guard.** Pub/Sub delivers at least once, so your handler will run twice. Without a create-only write on `(case_id, action_type, attempt)`, a retry files the same woman's appeal a second time.

**Eight identities, enforced in two places.** Intake can't read the policy bucket. Verification can't write drafts. IAM scopes the buckets and topics, and since Firestore has no collection-level IAM, a deterministic gateway enforces the same matrix in code.

All the data is synthetic: Synthea patients, encounters we wrote, and an invented payer whose six policies (42 sections, 113 citable identifiers) follow the structure of real published ones.

## Challenges we ran into

**Our own gateway had doors we didn't know about.** The docstring said every datastore consumer routed through it. Four didn't, including one in the same file the docstring named. One of them was writing to `quarantine` under an identity that only has READ on it, and it worked precisely because it skipped the check that would have refused it.

**A page that passed 763 tests returned 500 in production.** `/fleet` reads the agent roster at request time, and `infra/` is excluded from the container image. Locally the repository is the filesystem, so nothing caught it, and we'd just changed the front page to link there. Our tests now compare what the app opens at runtime against what actually ships in the image.

## Accomplishments that we're proud of

**The verifier catches things we never told it to look for.** Gemini described a CASE-001 encounter as a "telehealth evaluation." The chart calls it an "interim review" and never says how it happened. Verification killed the draft on the first attempt, then caught the same thing again on a separate run two days later.

**No fabricated citation has ever reached a human.** When Drafting invented a policy section that doesn't exist (`NBH-CARD-014-9.9`), Verification threw the draft out without spending a single model call.

**A case escalated itself while nobody was watching.** CASE-006's response window lapsed, and a scheduler tick moved it up to peer-to-peer review, rung 2 of 4. Nobody requested it, nothing was running in between, and no person was involved.

## What we learned

**Google's own guardrail missed an attack that ours caught, and we're leading with that.**

We fed the pipeline a denial letter with a hidden instruction telling the reader to approve the claim and forward the chart somewhere else. **Model Armor came back `NO_MATCH_FOUND`.** All four filters.

It's a fair miss. Read as a prompt, that letter is an ordinary business document with some imperative sentences in it. Our deterministic layer knows what a denial letter is supposed to look like, and it caught the thing on seven findings and quarantined it.

Everyone says defense in depth. Here the layers actually failed independently, which is the only thing that phrase has ever meant.

**Then our own checker killed a sound appeal three times, and two of those were wrong.**

That's CASE-003, the 83-year-old. We went back and read the rejections against the policy by hand. One of them objected to a sentence that was the criterion word for word with "Requires that" stuck on the front. Her appeal died over paraphrasing, and our safety cap turned a false positive into a lost appeal.

Our harness scored outcomes and grounding. It had no way of measuring how often the checker itself is wrong. We're publishing that instead of quietly re-running until CASE-003 came out clean, because a safety net with an unmeasured false positive rate is half a result, and the half you can't see is the half that costs someone their scan.

**What we can measure**, against live models on 28 Aug 2026:

* **8/8** fully grounded, and **0** fabricated citations reaching a human
* **6/8** outcomes as expected, and we publish the two that weren't
* **About 11 model calls per appeal produced** (44 calls across 8 cases, 4 of which correctly produced no appeal at all)

Refusing to appeal something that shouldn't be appealed is the product, not overhead.

## What's next for Overturn

Measuring that false positive rate we just admitted we can't quantify, because the appeals it wrongly kills are the ones nobody ever hears about.

Somewhere today a letter isn't getting written for a claim that would have been overturned. That's the letter this is for.
