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

Measured 2026-08-22 against the six-policy corpus, TF-IDF over unigrams plus
order-independent word pairs within a three-word window, cosine similarity:

| Case | Expected policy | Retrieved | Top score | Runner-up | Margin |
|---|---|---|---|---|---|
| CASE-001 | NBH-ENDO-031 | NBH-ENDO-031 | 0.111 | 0.029 | 0.082 |
| CASE-002 | NBH-MSK-022 | NBH-MSK-022 | 0.192 | 0.033 | 0.158 |
| CASE-003 | NBH-CARD-014 | NBH-CARD-014 | 0.093 | 0.042 | 0.051 |
| CASE-004 | *(none)* | — | 0.048 | 0.013 | 0.035 |
| CASE-005 | NBH-PULM-008 | NBH-PULM-008 | 0.223 | 0.009 | 0.214 |
| CASE-006 | NBH-BEHV-045 | NBH-BEHV-045 | 0.160 | 0.008 | 0.153 |
| CASE-007 | NBH-ENDO-031 | NBH-ENDO-031 | 0.125 | 0.039 | 0.086 |
| CASE-008 | NBH-CARD-014 | NBH-CARD-014 | 0.092 | 0.067 | 0.025 |

Weakest correct match 0.092; strongest no-policy match 0.048. Separation 0.044.

| Threshold | Value | Why |
|---|---|---|
| `retrieval_no_policy_floor` | 0.06 | Above every no-policy case, below every real one. Below this, the system declines to appeal rather than appealing weakly. |
| `retrieval_score_floor` | 0.11 | Just above the weakest correct matches, so CASE-003 and CASE-008 reformulate. A reformulation path that never fires is untested code. |

### Three things this measurement caught

Worth recording, because each one would have shipped silently and none of them
would have produced an error.

**1. Unigrams cannot separate two MRI policies.** A cardiac MRI denial retrieved
the *lumbar spine* policy. "Magnetic", "resonance" and "imaging" appear in both,
and across six documents they carry almost no inverse document frequency.

**2. Ranking policies by summed section score is wrong.** After adding word
pairs, the correct policy had the single best-matching section and still lost,
because the competing policy had more mediocre sections inside the top k.
Summing rewards corpus shape rather than relevance. Ranking by the strongest
single section, with the sum only as a tiebreak, fixed it.

**3. Word pairs have to be order-independent.** A CPT descriptor reads "Magnetic
resonance imaging, cardiac, with contrast". The policy it should match reads
"cardiac magnetic resonance imaging". Ordered adjacent bigrams share *nothing*
between those two strings. Unordered pairs within a three-word window share
three, which is what finally separated the two policies on every case.

The compression in the scores between the ordered and unordered versions is
real — the separation narrowed from 0.087 to 0.044 — and it is the right trade.
Word order varies between the two sides of this match by construction, because
a billing code descriptor and a medical policy are written by different people
for different purposes.

### Why the calibration script fails rather than suggesting

If the correct-policy and no-policy score populations overlap, the script exits
non-zero and refuses to suggest a threshold. A threshold can only be chosen once
retrieval is actually right. Choosing one while the populations overlap is how
you get a retriever that works on the eight cases someone happened to try.

The script also calls `TfidfIndex.best_policy` rather than reimplementing the
ranking. An earlier version reimplemented it, the two implementations drifted
apart, and the calibration reported success while the agent retrieved the wrong
policy. A measurement harness that does not exercise the real code path measures
the harness.
