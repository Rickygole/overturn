# Devpost submission — paste-ready

Every field below is verified against the deployment and the repo as of
2026-08-29. Copy each block into the matching Devpost field. Nothing here
needs editing except the video URL, which does not exist yet.

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
Seven agents read a health insurance denial, find the insurer's own published policy, check the chart against it criterion by criterion, and draft an appeal — then a second model tries to tear the draft apart before any human sees it.
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
The site is open. The review queue behind it is gated by one shared password,
published deliberately because everything behind it is synthetic:

    https://overturn-kruy6aauaq-uc.a.run.app
    password: northbeck-appeals-2026

Sign in and you land on the queue, which opens with a dashboard: eight cases
across six states, three of them refusals.

Worth clicking, in this order:

  CASE-001  Three drafts, two rejected. Verification caught the model claiming
            a 14 July encounter was a "telehealth evaluation" — the chart calls
            it an "interim review" and never says how it was conducted. Nobody
            planted that. The rejection text is on the page.
  CASE-003  Three drafts, three rejections, nothing sent -- and two of those
            three rejections are wrong. Verification objected to an accurate
            restatement of a policy criterion. We read the rejections against
            the policy text by hand and recorded it in docs/EVALUATION.md
            rather than presenting the case as a success. It is the clearest
            evidence we have that the checker's false-positive rate is real
            and that we have not yet measured it.
  CASE-002  Quarantined. A denial letter with an injected instruction in it.
            It never reaches the queue, which is why it is listed under
            "closed cases" rather than silently vanishing.
  CASE-006  Escalated. Submitted, the payer went silent, and Lifecycle moved it
            from first-level appeal to peer-to-peer review on its own.

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

### Inspiration

A denial letter lands on a billing clerk's desk, and it is the fortieth one
this month. Appealing it properly means finding the insurer's own medical
policy, reading the criteria, opening the patient's chart, deciding which
criteria the record actually supports, and writing a letter that quotes both
by identifier. That is forty minutes of skilled work for one claim.

So most denials are never appealed. Not because the care was wrong — because
nobody had forty minutes. A large share of the denials that *are* appealed get
overturned, which means many of the ones nobody appeals were winnable.

### What it does

Overturn runs that research task as a fleet of seven single-purpose agents.
Sentinel screens the incoming document for injected instructions before
anything reads it as instructions. Intake extracts the claim. Retrieval finds
the payer's published policy, and stops the case entirely if no policy applies.
Mapping rules on each criterion against the chart with the locator that
supports it. Drafting writes the appeal. Verification tries to break it.
Lifecycle holds the case for weeks and escalates when the payer's clock runs
out.

Two humans sign before anything is transmitted — a clerk on the paper trail, a
clinician on the medicine — and both signatures must land on the same draft
attempt. Revise the letter and both are void.

### The hard part is not the writing

A language model will happily write a persuasive appeal citing a policy section
that does not exist. A fabricated citation is worse than no appeal: it costs
the clinic credibility with the payer on every future claim. So the system is
built around that failure rather than around the drafting.

Drafting has no retrieval tool. It sees the criteria matrix and the retrieved
policy text and nothing else, so an invented fact has no source it could
plausibly have come from. Verification runs on a separate model and can reject
and explain but cannot edit — it has no path to writing its way to a pass.
After three rejected attempts the case goes to a human with the reasons
attached and **nothing is sent**. Failing to send is a designed outcome.

### What we got wrong, and found by reading it

The same check that catches real overreach also rejects restatements that are
accurate. On the deployed CASE-003 it killed a well-founded cardiac MRI appeal
three times over paraphrase -- once objecting to a sentence that was verbatim
the policy criterion with "Requires that" in front of it. The attempt cap then
turned a false positive into a lost appeal rather than a bad letter.

We found that by reading the rejections against the policy text by hand, not
from the harness, which scores outcomes and grounding and has no measure of how
often the checker is wrong in the other direction. That gap is stated in
docs/EVALUATION.md. A safety net with an unmeasured false-positive rate is half
a result, and saying so is more useful than a number that would not survive a
re-run.

### The finding this project exists to report

CASE-001 is the case the manifest calls easy. Offline, with a scripted backend,
it drafts cleanly on the first attempt. Real Gemini had an opinion of its own,
and it was wrong in a specific, checkable way: attempt 1 asserted that the
patient's 14 July 2026 encounter was a **telehealth evaluation**. The chart
records it as an "interim review" and never says how it was conducted.

Nobody prompted anyone to look for this. Verification caught it:

> The chart does not establish that the evaluation on July 14, 2026 was a
> telehealth evaluation, nor does it mention the timing of the request to
> establish it was within six months.

The draft went back with that as revision instructions and the next attempt
dropped the claim.

Then it happened again. The deployed run — a **different backend**, a different
process, a different day — produced the same overclaim on the same case, and
caught it again. That is not two inconsistent runs. It is a reproducible
failure mode with a reproducible detector, which is stronger evidence than
either run alone.

### How I built it

Seven ADK agents on Vertex AI. `gemini-3.5-flash` for extraction, mapping and
verification; `gemini-3.7-flash` for drafting, the one step where output
quality is the product; `gemma-4-26b-a4b-it-maas` as Sentinel's guard model;
`text-embedding-005` for retrieval.

ADK owns every model call — tool schemas, structured output, retries. The
orchestration is deliberately not ADK's, because an ADK Runner session cannot
survive the multi-week gap between submitting an appeal and a payer answering
it. The case state lives in a Firestore document instead, so any worker can
resume a case cold, weeks later, having never seen it.

The Fortified Enterprise Fleet components, on primitives where no managed
surface was reachable on this project (documented in `docs/PLATFORM_PROBE.md`):

- **Agent Registry** — a versioned catalogue where every field is *derived*
  from the same sources the running code uses, so an entry cannot drift from
  the agent it describes. A wrong catalogue about who may touch patient data is
  worse than no catalogue.
- **Memory Bank** — cross-case memory of payer behaviour, keyed on payer,
  policy and denial reason code, and never on a patient.
- **Agent Identity** — eight service accounts, one per role.
- **Agent Gateway** — Firestore has no collection-level IAM, so per-agent
  scoping is enforced in code and backed by tests that assert the absences.
- **Model Armor** — one of Sentinel's three screening layers, running in the
  deployment.
- **Observability** — an OpenTelemetry span per agent invocation, exported to
  Cloud Trace, so one trace shows a case's whole reasoning chain including the
  drafting/verification retries nested under it.

Infrastructure: Cloud Run, Pub/Sub with a dead-letter topic, Firestore, Cloud
Storage, Cloud Scheduler, Secret Manager. A letter dropped in a bucket is the
only input.

### Challenges

**Gemini 3.x is served only from the `global` endpoint.** Every regional
endpoint returns 404 while the Model Garden catalogue lists the models as GA in
those regions. Infrastructure stays in `us-central1`; model calls go to
`global`. That cost a day and is now two separate settings for that reason.

**Gemma ignores a bound `response_schema`.** Gemini enforces the shape on the
wire; Gemma does not. Asked for a full result object including fields the model
never sees, it returned valid JSON containing one of them — so every run logged
`gemma:unavailable` and Sentinel's three-layer claim was one layer in practice.
The fix was JSON mode without a bound schema, a narrower schema asking only for
what the guard model can know, and tolerant parsing for the three response
shapes actually observed.

**An action-key collision made every clean case permanently un-approvable.**
The idempotency guard keyed approvals and rejections under one action type, so
recording one blocked the other forever. Splitting them was the fix; a
red-team pass found it, not a test.

**The escalation ladder climbed zero rungs** because `requires_human` had been
conflated with halting. My own test passed only because it set the appeal level
by hand.

### What I learned

**A documentation surface can outgrow its verification harness.** I had tests
asserting the README matched the code. The published site was HTML, outside
every one of them, and it accumulated claims the code did not support —
including a provenance feature that did not exist, on a project whose entire
thesis is provenance. A claim is not safer for being written in HTML. There is
now a harness over the site too, and it caught four false claims the moment it
existed.

**The offline backend was quietly lying about its own identity.** It is handed
the configured model name and hands it straight back, so a draft assembled by a
regex stub reached the review screen labelled "Generated by gemini-3.7-flash."
On the one screen whose job is helping a person decide whether to trust a
letter, that is the worst possible place to be casually wrong.

**Two runs disagreeing is data, not embarrassment** — if you can say what was
the same about them.

### What's next

Agent-to-agent delegation rather than a fixed order; concurrent case handling;
and a real payer integration behind the simulator boundary that already exists
for it.

---

## Built with

```
google-adk, google-genai, gemini-3.5-flash, gemini-3.7-flash, gemma, vertex-ai,
text-embedding-005, cloud-run, firestore, pub-sub, cloud-storage,
cloud-scheduler, cloud-trace, model-armor, secret-manager, opentelemetry,
python, fastapi, pydantic, jinja, uv, pytest, docker, synthea
```

---

## Bonus contributions to declare

- **Additional Google AI model:** Gemma (`gemma-4-26b-a4b-it-maas`) runs as
  Sentinel's guard layer. Worth 0.2 — say so explicitly in the writeup, because
  a judge has to notice it to award it.
- **Blog post:** publish `docs/BLOG_POST.md`. It already carries the required
  sentence saying it was written for this hackathon. Must be public, not
  unlisted.
- **Social post:** the LinkedIn copy is in `docs/PITCH.md`, already carrying
  `#AllThingsAgenticHackathon`.
