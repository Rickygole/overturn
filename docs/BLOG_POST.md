---
title: "The Bug That Would Have Sent a Perfect Cardiac Appeal to the Wrong Policy"
published: false
description: "Six real bugs from building Overturn, an agent fleet that appeals denied health insurance claims for weeks without a human re-prompting it — written for the Google + Devpost All Things Agentic Hackathon."
tags: ai, agents, python, healthtech
---

*This post was written for the Google + Devpost **All Things Agentic Hackathon**
(track: The Fortified Enterprise Fleet), to satisfy the "explain how it was
built" bonus. Everything below happened while building
[Overturn](https://github.com/Rickygole/overturn).*

## What it does

A clinic gets a denial letter from an insurer. Appealing it properly means
finding the payer's own published medical policy, checking the patient's chart
against that policy line by line, and writing a letter that cites section
numbers correctly. That's maybe forty minutes of skilled work per claim, which is
why most winnable denials never get appealed at all — the labor cost exceeds
the value of the claim.

Overturn is seven agents that do that work end to end: read the letter,
retrieve the governing policy, map the chart against its criteria, draft the
appeal, verify every citation before a human sees it, and then — the part that
actually matters — sit on the case for weeks and escalate on its own when the
payer's response deadline passes in silence. No open connection, no process
running, no in-memory object holding anything together. A worker that has
never seen the case wakes up on a schedule, reconstructs it from Firestore, and
keeps going.

That last property is why most of the bugs below are boring-sounding until you
think about what they'd have looked like three weeks into a real case instead
of five minutes into a demo.

## 1. The escalation ladder could only climb one rung

Lifecycle finds cases whose deadline has passed and moves them up a ladder:
submitted → escalated → appealed-to-external-review → whatever's next. Early on,
the query that found overdue cases checked for `status == "submitted"`. The
escalation action itself sets `status = "escalated"`.

Run it once and it works. Run it twice and the second overdue deadline is
invisible — the case is sitting in `escalated`, past its new deadline, and
nothing is looking for it anymore. The ladder has four rungs and the code could
only ever find you on the first one.

This is exactly the failure the product exists to prevent — a case going quiet
because nobody's watching it — reintroduced by the one component whose entire
job is to watch it. It's also invisible in any demo shorter than the appeal
window, which is weeks. You only find it by asking "what does the query select
after the first successful run," not by running the pipeline once and watching
it work.

## 2. Computed fields that couldn't survive a round trip

Case state is a Pydantic model with a few `@computed_field` properties —
things like "is this overdue" derived from other fields. Convenient to read.
The problem: `to_firestore()` serializes computed fields into the stored
document like any other field, and the schema is `extra="forbid"`. Load the
same document back and validation rejects it, because now there's a field on
the wire that isn't a declared input field.

Write, then read, then crash. In a system whose entire premise is "pick this
case back up in three weeks," a record that can't survive its own round trip
isn't a record — it's a live variable pretending to be one. The fix was
mechanical (exclude computed fields from the stored payload, recompute on
load), but finding it meant actually writing a case, restarting the process,
and reading it back, rather than trusting that the schema round-trip tests I'd
already written covered every model equally. They didn't; the models with
computed fields needed their own test.

## 3. Retrieval sent a cardiac MRI denial to the lumbar spine policy

This is the one I'd lead with if I could only pick one.

`scripts/calibrate_retrieval.py` runs the real retrieval code against eight
labeled cases and refuses to suggest a score threshold unless the correct-policy
and no-policy score populations are cleanly separated. Early on, it exited
non-zero: a cardiac MRI denial was retrieving the lumbar spine MRI policy, with
a *higher* score than the correct one. Three separate causes stacked on top of
each other:

- **Unigrams can't separate two MRI policies.** "Magnetic," "resonance," and
  "imaging" appear in both documents, and across a six-document corpus they
  carry almost no inverse document frequency. TF-IDF has nothing to grab onto.
- **Ranking by summed section score is wrong.** Once I added word-pair
  features, the correct policy had the single best-matching section in the
  whole corpus — and still lost the ranking, because the wrong policy had more
  mediocre-scoring sections inside the top-k and summing rewarded that. Fix:
  rank by the single strongest section, use the sum only as a tiebreak.
- **Word pairs have to be order-independent.** A CPT descriptor reads
  *"Magnetic resonance imaging, cardiac, with contrast."* The policy text
  reads *"cardiac magnetic resonance imaging."* Ordered adjacent bigrams share
  zero terms between those two strings. Unordered pairs within a three-word
  window share three, which is what finally separated the two MRI policies on
  every test case.

The measured effect of switching from ordered to unordered pairs is real and
it's a cost, not a free win: separation between the weakest correct match and
the strongest wrong match narrowed from 0.087 to 0.044. I took that trade
anyway, because 0.044 with the right document beats a wider margin pointed at
the wrong one.

The part that actually worried me: every downstream agent would have done
careful, well-cited work on the lumbar spine policy. Mapping would have
produced a clean criteria matrix. Drafting would have written a well-argued
letter. Verification checks that every cited section id exists in the
retrieved set and that quotes match the source text — and all of that would
have passed, because the citations are completely correct against the document
Retrieval handed them. The letter would have been internally consistent and
entirely irrelevant, and nothing downstream is positioned to catch "this is
the wrong policy" because no agent downstream ever sees the alternative.

That's why the calibration script fails loudly instead of picking a threshold
that merely worked on the cases someone happened to try. And it calls the real
`TfidfIndex.best_policy` function rather than reimplementing the ranking logic
for measurement purposes — an earlier version did reimplement it, the two
copies drifted apart, and the calibration reported success while the actual
agent kept retrieving the wrong policy. A harness that doesn't exercise the
real code path measures the harness, not the system.

## 4. The safety filter's false positives were the real risk

Sentinel screens every inbound denial letter for prompt injection before
anything else touches it, using regex rules that look for instruction-shaped
language rather than any specific payload. One rule flags an insurer telling
the reading system to close or drop a case. Early version of that pattern
matched on `close|drop|abandon` near `this case` — which also matched a
completely ordinary sentence in a real denial letter: *"the plan will not
close this case until the appeal period has run."*

A false negative here lets an injected instruction through. A false positive
here quarantines a legitimate denial letter and halts the appeal — which, for
a product whose stated purpose is getting winnable appeals filed instead of
dropped, is close to the worst possible failure mode. The fix was requiring
imperative mood: the pattern now anchors on sentence-initial or
clause-initial position (`_IMPERATIVE_START` in `agents/sentinel/rules.py`),
so it catches *"Close this case"* as an instruction but not *"will not close
this case"* as a statement about the future. Getting the false-positive rate
down mattered exactly as much as the false-negative rate, and I hadn't
budgeted attention for that going in.

## 5. Every Pro-tier model in the catalog was below the hackathon's floor

Small one, but it's the kind of thing that's easy to fudge quietly. The rules
require Gemini 3.5 or newer. I'd planned one deliberately expensive call —
drafting the actual appeal letter — on a Pro-tier model, reasoning that output
quality is the one place in the pipeline where it's worth paying for it.

Checking the catalog: every 3.x Pro-tier id (`gemini-3-pro-preview`,
`gemini-3.1-pro-preview`) is in public preview and numbered *below* 3.5.
`gemini-2.5-pro` is GA and would clear the bar on raw capability, but 2.5 fails
the version floor outright. There is no Pro model in this catalog that
qualifies. So "use Pro for the hard call" became "use the newest GA Flash
model," `gemini-3.7-flash`. The tempting move would have been to quietly fall
back to `gemini-2.5-pro` because it's a better model and nobody demoing the
video would notice the id in the logs. Writing the actual reasoning down in
`docs/MODEL_CHOICES.md` — including the rejected candidates — felt more
useful than a marginally better draft on a disqualifying model.

## The rule that ended up shaping everything

Somewhere around the second or third of these, I wrote down a rule and then
went back through the whole codebase to enforce it: **never ask a model a
question Python can answer.**

*Does this cited section id exist in the retrieved set* is a set-membership
check. *Has the payer's deadline passed* is a timestamp comparison. *Does this
evidence quote actually appear at the chart locator it claims* is a substring
match after normalizing whitespace and smart quotes. None of these can
hallucinate, because none of them are ever asked to a model — they're plain
Python in `agents/mapping/validate.py`, `core/idempotency.py`, and
`core/gateway.py`. The model's job shrinks to the genuinely semantic
questions: does this source text support this specific clinical claim, does
this letter's language look like an instruction rather than correspondence.
Everything that can be phrased as membership, comparison, or string matching
got pulled out and made boring on purpose.

That's most of what this build actually was: finding the places where I'd
handed a model a question it had no business answering, and the places where
a component's second run behaved differently from its first.
