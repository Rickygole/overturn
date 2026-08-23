# Which agent uses which model, and why

Seven agents do not need seven model tiers. Most of this pipeline is extraction
and checking, which is cheap work. Exactly one step is genuinely hard.

## The floor

The hackathon requires Gemini 3.5 or newer. Every generative call in Overturn
is at or above that line. This constrained the choice more than it might look:

| Candidate | Catalog stage | Meets the 3.5 floor | Verdict |
|---|---|---|---|
| `gemini-3.7-flash` | GA | yes | **Chosen** for drafting |
| `gemini-3.6-flash` | GA | yes | Held in reserve |
| `gemini-3.5-flash` | GA | yes | **Chosen** as the workhorse |
| `gemini-3.5-flash-lite` | GA | yes | Considered for Sentinel, rejected — see below |
| `gemini-3.1-pro-preview` | Public preview | **no**, 3.1 < 3.5 | Rejected |
| `gemini-3-pro-preview` | Public preview | **no**, 3.0 < 3.5 | Rejected |
| `gemini-2.5-pro` | GA | no | Rejected |

The interesting consequence: there is no Pro-tier model in this catalog that
clears the 3.5 floor. Every 3.x Pro id is preview and numbered below 3.5. So the
"one justified Pro call" in the original plan becomes "one justified call to the
newest GA Flash", which is `gemini-3.7-flash`. Stating this rather than quietly
falling back to `gemini-2.5-pro`, which would have been the more capable model
and also a disqualification.

## Assignments

| Agent | Model | Why this tier |
|---|---|---|
| Sentinel | Model Armor + `gemma-4-26b-a4b-it-maas` + rules | Screening is a classification job. An open-weights model plus deterministic rules is both cheaper and easier to reason about than a frontier model, and Model Armor is the purpose-built layer. Three layers because any one of them alone has a false-negative rate we cannot measure. |
| Intake | `gemini-3.5-flash` | Multimodal field extraction from a PDF or a fax scan. Structured output does the heavy lifting; the model only has to read. |
| Retrieval | `gemini-3.5-flash`, called conditionally | Only invoked to reformulate the query when the first vector search scores below the floor. A good first retrieval costs zero model calls. |
| Mapping | `gemini-3.5-flash` | The analytical core, and the agent most likely to need escalation. Starting at Flash deliberately; if verdicts prove unstable across runs, this is the one agent that moves up, and the change gets recorded here with the evidence that prompted it. |
| Drafting | `gemini-3.7-flash` | The one call where output quality is the product. Newest GA model available. |
| Verification | `gemini-3.5-flash` + deterministic string matching | Citation existence is a set-membership test and is done in Python, not by a model — a model cannot be wrong about it if it is never asked. The model handles only the semantic question of whether source text supports a claim. |
| Lifecycle | `gemini-3.5-flash` | Chooses a rung of a four-rung ladder, constrained by a static table. A small decision with a small model. |

## Rules that constrain every call

- **No model is asked a question that Python can answer.** Does this section id
  exist in the retrieved set is a set operation. Has the deadline passed is a
  timestamp comparison. Both are done in code and both are therefore immune to
  hallucination.
- **Structured output everywhere.** Every generative call is bound to a Pydantic
  schema. A malformed response is a validation error at the boundary, not a
  surprise three agents downstream.
- **Escalations get written down.** If an agent moves to a larger model, the
  reason and the evidence land in this file. An undocumented model upgrade is
  indistinguishable from guessing.

## Recorded escalations

None yet. This section stays empty until an agent earns a move.

## Retrieval calibration

Retrieval has two thresholds and they answer different questions. Both are set
from measurement, not intuition. Reproduce with:

```bash
uv run python scripts/calibrate_retrieval.py
```

Measured 2026-08-22 against the six-policy corpus, TF-IDF with unigram and
bigram features, cosine similarity:

| Case | Expected policy | Retrieved | Top score | Runner-up | Margin |
|---|---|---|---|---|---|
| CASE-001 | NBH-ENDO-031 | NBH-ENDO-031 | 0.166 | 0.043 | 0.123 |
| CASE-002 | NBH-MSK-022 | NBH-MSK-022 | 0.231 | 0.069 | 0.162 |
| CASE-003 | NBH-CARD-014 | NBH-CARD-014 | 0.148 | 0.086 | 0.061 |
| CASE-004 | *(none)* | — | 0.052 | 0.027 | 0.025 |
| CASE-005 | NBH-PULM-008 | NBH-PULM-008 | 0.284 | 0.022 | 0.262 |
| CASE-006 | NBH-BEHV-045 | NBH-BEHV-045 | 0.197 | 0.017 | 0.180 |
| CASE-007 | NBH-ENDO-031 | NBH-ENDO-031 | 0.200 | 0.060 | 0.140 |
| CASE-008 | NBH-CARD-014 | NBH-CARD-014 | 0.139 | 0.093 | 0.045 |

Weakest correct match 0.139; strongest no-policy match 0.052. Separation 0.087.

| Threshold | Value | Why |
|---|---|---|
| `retrieval_no_policy_floor` | 0.08 | Above every no-policy case, below every real one. Below this, the system declines to appeal. |
| `retrieval_score_floor` | 0.16 | Just above the weakest correct matches, so CASE-003 and CASE-008 reformulate. A reformulation path that never fires is untested code. |

### What this measurement changed

The first version of the retriever used unigrams only and **retrieved the wrong
policy for CASE-008** — a cardiac MRI denial matched the lumbar spine MRI
policy, because "magnetic", "resonance" and "imaging" appear in both and carry
almost no inverse document frequency across six documents.

The two available responses were to lower the threshold until the wrong answer
counted as acceptable, or to fix the scorer. Adding bigram features fixed it:
"cardiac magnetic" appears in exactly one policy. All eight cases now retrieve
correctly.

This is the reason the calibration script prints the separation between the two
populations and **fails when they overlap** rather than emitting a suggested
threshold. A threshold can only be chosen once the retrieval is right; choosing
one while they overlap is how you get a system that works on the cases someone
happened to try.
