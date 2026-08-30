# Overturn

**An agent fleet that appeals wrongful health insurance denials, and keeps
appealing for weeks without being asked twice.**

A clinic billing clerk gets a denial letter. Today they either spend forty minutes
writing an appeal, or — far more often — they don't, and the claim dies. Marketplace
insurers denied about 85 million in-network claims in 2024. Consumers appealed
262,982 of them — fewer than one in three hundred — and insurers overturned 34%
of the appeals they received ([KFF, ACA Marketplace claims data, 2024][kff]).
A third of the people who bother, win; almost nobody bothers. The bottleneck is
not judgment. It is that the labour cost of one appeal exceeds the value of one
claim.

[kff]: https://www.kff.org/patient-consumer-protections/claims-denials-and-appeals-in-aca-marketplace-plans-in-2024/

Overturn reads the denial letter, finds the insurer's own published medical
policy, checks the patient's chart against that policy criterion by criterion,
drafts an appeal citing the policy by section number, verifies every citation is
real before a human ever sees it, and then holds the case for weeks — escalating
on its own when the payer's response deadline passes in silence.

A human approves before anything is transmitted. That gate is a design decision,
not a limitation.

**Everything here is synthetic.** The patients, the payer ("Northbeck Health
Plan"), its policies, and every denial letter are invented for this project.
No real patient data and no real insurer appear anywhere — see "Data sources
and compliance" below.

**Live on Google Cloud:** the human approval interface is hosted at
[`https://overturn-kruy6aauaq-uc.a.run.app`](https://overturn-kruy6aauaq-uc.a.run.app),
gated by a single shared password rather than a Google account —
**`northbeck-appeals-2026`** — because it renders what looks like a medical
record and an open URL with no door at all would undercut the point of the
project. `overturn-ingest` and `overturn-scheduler` are also live but
deliberately private: Pub/Sub and Cloud Scheduler invoke them, not a browser,
so a 403 from either of those two `.run.app` URLs is correct, not a broken
deployment. See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for how all three were
deployed.

> **Overturn does not decide whether care is appropriate.** It determines whether
> the documentation in a chart matches criteria the payer has already published.
> That is a documentation-matching problem, not a clinical one, and the
> distinction is enforced in the code: no agent is permitted to assert a clinical
> fact that does not trace to a row in the criteria matrix.

---

## Status

Built for the Google + Devpost **All Things Agentic Hackathon**, track:
*The Fortified Enterprise Fleet*. This section is updated as the build lands.

| Layer | State |
|---|---|
| Typed inter-agent contracts | done — 30 exported models across 40 exported names, strict, round-trip tested |
| Per-agent access gateway | done |
| Idempotency guard | done — leases, replay, payload-drift detection |
| Append-only audit log | done |
| OpenTelemetry tracing | done |
| Document store (Firestore + offline) | done |
| Policy corpus | done — 6 policies, 42 sections, 113 citable identifiers |
| Patient charts | done — Synthea base plus authored encounters |
| Seven agents | done |
| Cloud deployment | done — three Cloud Run services, live (see above) |

---

## The seven agents

Each agent is a separate identity with its own service account and its own
scope. The boundaries are enforced, not described.

| # | Agent | Does | Notably cannot |
|---|---|---|---|
| 1 | **Sentinel** | Screens the inbound document for prompt injection, instruction-like content, and unexpected PII. Can quarantine and halt the pipeline. | Read the policy corpus or any case detail. It handles untrusted bytes and gets the smallest surface in the fleet. |
| 2 | **Intake** | Extracts structured fields from the letter — payer, claim number, service, denial reason, deadline. Handles PDFs and scanned faxes. | See the policy corpus. Extraction must not be able to consult the answer key. |
| 3 | **Retrieval** | Finds the governing policy sections and returns them with stable identifiers and verbatim text. | Write anything but its own result. |
| 4 | **Mapping** | For each policy criterion, returns satisfied / not satisfied / insufficient documentation, with the chart evidence and a locator pointing at where in the chart it came from. | Draft prose. |
| 5 | **Drafting** | Writes the appeal from the satisfied criteria only, citing sections by identifier. | **Retrieve anything.** It has no access to the policy corpus, so it cannot go looking for supporting material Mapping did not hand it. |
| 6 | **Verification** | Checks every citation three ways before a human sees the draft. | Write the draft it is judging. |
| 7 | **Lifecycle** | Runs on a schedule, never in the request path. Finds cases whose payer deadline has passed and climbs the appeal ladder. | Skip a rung. The ladder is a static table, not a model decision. |

### What each identity can actually reach

Generated from `core.gateway.POLICY` — the same dict the gateway enforces at
runtime — so this table cannot drift from what the code does. Regenerate with
`uv run python scripts/seed_registry.py`.

| Agent | Service account | Model | Returns | Reads | Writes |
|---|---|---|---|---|---|
| `sentinel` | `overturn-sentinel` | `gemma-4-26b-a4b-it-maas` | `—` | `audit_events`, `cases`, `quarantine` | `audit_events`, `quarantine` |
| `intake` | `overturn-intake` | `gemini-3.5-flash` | `DenialExtraction` | `audit_events`, `cases` | `audit_events`, `cases` |
| `retrieval` | `overturn-retrieval` | `gemini-3.5-flash` | `RetrievalResult` | `audit_events`, `cases`, `policy_sections` | `audit_events`, `cases` |
| `mapping` | `overturn-mapping` | `gemini-3.5-flash` | `CriteriaMatrix` | `audit_events`, `cases` | `audit_events`, `cases` |
| `drafting` | `overturn-drafting` | `gemini-3.7-flash` | `AppealDraft` | `audit_events`, `cases` | `audit_events`, `cases` |
| `verification` | `overturn-verification` | `gemini-3.5-flash` | `VerificationResult` | `audit_events`, `cases`, `policy_sections` | `audit_events` |
| `lifecycle` | `overturn-lifecycle` | `gemini-3.5-flash` | `EscalationDecision` | `actions`, `audit_events`, `case_memory`, `cases` | `actions`, `audit_events`, `case_memory`, `cases` |

Two rows are the point of the whole table. **Drafting** appears with no read on
`policy_sections`, which is why it cannot go looking for supporting material
Mapping did not hand it. **Verification** appears with no write on `cases`,
which is why it cannot edit the draft it is judging. Neither is a rule someone
has to remember; the handle an agent receives will not perform the call.

A note on honesty, because it matters more than the table looks good. Buckets,
Pub/Sub topics and secrets are genuinely scoped per identity by
`infra/iam_setup.sh`, and an agent without the grant cannot reach them. Firestore
has no collection-level IAM, so collection scoping is enforced deterministically
in `core/gateway.py`, which every datastore consumer routes through — agents
receive a `GatewayHandle`, never a store. Both layers are real; neither is
claimed to be the other.

## How a case actually moves

```
denial letter → Cloud Storage → Pub/Sub → Cloud Run
                                             │
                            Sentinel ────────┤ quarantine? → halt, audit, notify
                            Intake           │
                            Retrieval        │ no governing policy? → decline, say why
                            Mapping          │
                            Drafting ⇄ Verification   (max 3 attempts)
                                             │
                                  HUMAN APPROVAL GATE
                                             │
                                        submitted
                                             │
              ══════════ nothing is running ══════════
                     state lives in Firestore alone
                                             │
                        Cloud Scheduler wakes a job
                            Lifecycle → escalate → new deadline
                                             │
                                          repeat
```

The gap in the middle is the part that matters. There is no process, no open
connection, and no in-memory object holding the case together between the
appeal going out and the payer answering weeks later. A worker that has never
seen the case reconstructs it from one document and continues.

## Three properties this was built around

**1. The multi-week state is the product.** Anyone can write a loop with a sleep
in it. Durable state plus a scheduler plus idempotent handlers is how a system
survives weeks, restarts, and redeliveries.

**2. The verification layer means it refuses to lie.** Every cited identifier is
checked for existence against the retrieved set — in Python, as set membership,
because a model cannot be wrong about a question it is never asked. Then the
cited source text is checked against the claim the letter makes about it, and
every clinical assertion is checked against the criteria matrix. A failure
rejects the draft and the specific reason is fed back as revision instructions.

**3. The input is hostile by default.** A denial letter is a document from an
outside party. Treating its contents as data rather than as instructions is
Sentinel's entire job, which makes prompt-injection defence load-bearing here
rather than a checkbox.

## Does it actually get the right answer?

The failure worth worrying about in a system like this is not a crash. It is a
confident, well-cited, entirely irrelevant appeal — and nothing that asserts on
status codes would notice one.

So correctness is measured, not assumed. `scripts/evaluate.py` runs every case
end to end and checks the outcome against what the scenario is *supposed* to
produce, then independently re-checks every citation against the retrieved
policy and every chart quote against the chart. It re-derives that grounding
itself rather than asking the verification layer, because a harness that trusts
the component it is measuring measures nothing.

```bash
uv run python scripts/evaluate.py          # offline backend, deterministic, free
uv run python scripts/evaluate.py --live   # real Vertex AI, costs money
```

**These numbers are from the offline backend**, and that matters when reading
them. The deterministic parts are the real thing — Sentinel's rules, the
citation-existence check, retrieval, the whole orchestration — but the
generative calls are answered locally, and `mapping.map_section` in particular
reads verdicts from a fixture. So this table says the pipeline reaches the right
conclusions given correct clinical judgements; it does not say the model makes
them. `docs/EVALUATION.md` reports the same eight cases against real Gemini,
which is the number that answers that question.

| Case | Scenario | Expected | Reached | Fabricated citations | Unlocatable evidence |
|---|---|---|---|---|---|
| `CASE-001` | clean win | `awaiting_human_approval` | `awaiting_human_approval` | 0 | 0 |
| `CASE-002` | prompt injection | `quarantined` | `quarantined` | 0 | 0 |
| `CASE-003` | verification catch | `awaiting_human_approval` | `awaiting_human_approval` | 0 | 0 |
| `CASE-004` | no applicable policy | `declined_no_basis` | `declined_no_basis` | 0 | 0 |
| `CASE-005` | clean win | `awaiting_human_approval` | `awaiting_human_approval` | 0 | 0 |
| `CASE-006` | insufficient documentation | `declined_no_basis` | `declined_no_basis` | 0 | 0 |
| `CASE-007` | scanned fax | `awaiting_human_approval` | `awaiting_human_approval` | 0 | 0 |
| `CASE-008` | second denial still undocumented | `declined_no_basis` | `declined_no_basis` | 0 | 0 |

```
outcomes correct              8/8
cases fully grounded          8/8
citations checked             29
citations not in the corpus   0
chart quotes with no locator  0
ok   transient fabrication caught, retry clean            2 attempt(s), ended awaiting_human_approval
ok   persistent fabrication stops at the cap, nothing sent 3 attempt(s), ended needs_human_review
```

Four of those rows are the ones that matter, and three of them are refusals.

**CASE-002** never reaches extraction at all. **CASE-004** is a denial no policy
in the corpus governs, so there is no criterion to argue against.

**CASE-006** and **CASE-008** are the interesting ones. Both have charts that
document most criteria beautifully and are silent on the one the payer actually
denied on. No count of satisfied criteria separates those from a real appeal —
CASE-006 documents seven of nine — so appealability is decided by whether the
chart answers the criterion the payer's own stated reason turns on. Both decline,
and both tell the clerk which note to go and get:

> The payer denied on NBH-CARD-014-3.5, and the chart is silent on exactly that.
> Other criteria are well documented, but none of them answer the question that
> was actually asked. The gap is in the documentation rather than in the
> determination — the useful next step is to obtain that note, not to send an
> appeal that argues around it.

## Recovery, specifically

The rubric asks how the system recovers if a worker agent loops or returns a
hallucination. Directly:

- **A hallucinated citation** is caught by Verification's existence check before
  any human sees the draft, the draft is rejected, and the failure text becomes
  the next attempt's revision instructions. After three attempts the case is
  flagged for a person rather than sent. `core/schemas/verification.py`
- **A looping agent** is bounded by `max_verification_attempts`, and the retry
  count is an attribute on the trace span, so a loop is visible rather than
  merely survived.
- **A duplicate delivery** is absorbed by the idempotency guard: every action
  with an external effect is claimed under `case_id:action_type:attempt` with a
  create-only write, and a second delivery replays the stored result instead of
  re-executing. `core/idempotency.py`
- **A worker that dies mid-action** leaves a claim that would otherwise block
  that action forever, so claims carry a lease and an expired claim can be taken
  over. The takeover is recorded.
- **A crash mid-pipeline** loses nothing, because every stage commits its output
  to the case document before the next stage begins.
- **Two workers racing the same case** cannot silently lose an update; case
  writes are optimistic-locked on a revision counter.

## Repository layout

```
agents/           one package per agent
core/             contracts and plumbing shared by all of them
  schemas/        every inter-agent contract, strict and round-trip tested
  gateway.py      per-agent collection access policy
  idempotency.py  the guard against duplicate external effects
  state.py        durable case state, optimistic locking
  audit.py        append-only event log
  llm.py          model access, Vertex and offline backends
  store.py        Firestore and in-memory document stores
  telemetry.py    OpenTelemetry spans and Cloud Trace export
services/         Cloud Run surfaces: ingest, approval UI, scheduler job
data/             synthetic policy corpus, charts, denial letters
infra/            IAM, API enablement, deploy scripts
docs/             architecture, model choices, platform probe
```

## Running it locally

**Nothing here requires a cloud project, an API key, or a network connection.**
The full seven-agent pipeline runs offline and deterministically, so the
architecture can be exercised rather than described. Only the generative calls
are answered locally; the orchestration, the retry loop, the idempotency guard,
the access gateway, the audit trail and the trace spans are all the real thing.

```bash
git clone https://github.com/Rickygole/overturn.git
cd overturn
uv sync                # needs uv; see https://docs.astral.sh/uv/
uv run pytest -q       # 721 tests, a few seconds
```

Verified from a clean clone on 2026-08-23. If those two commands do not both
succeed, that is a bug in this README and not in your machine.

### Watch it work

**A clean case.** A denial letter goes in, a verified appeal comes out and stops
at the human gate:

```bash
uv run python scripts/run_pipeline.py CASE-001
```

**A prompt injection.** The letter carries an instruction payload aimed at
whatever reads it. The pipeline halts before Intake ever sees the text:

```bash
uv run python scripts/run_pipeline.py CASE-002
```

**A fabricated citation, caught.** Drafting is deliberately instructed to invent
a policy section on its first attempt. Verification rejects the draft, feeds
back the specific reason, and the retry is clean:

```bash
OVERTURN_SABOTAGE_DRAFTING=first uv run python scripts/run_pipeline.py CASE-003
```

**The same fault, made permanent.** Three rejections, then the case goes to a
person and nothing is sent:

```bash
OVERTURN_SABOTAGE_DRAFTING=always uv run python scripts/run_pipeline.py CASE-003
```

That fault-injection switch is off unless the environment variable is set for a
single deliberate run. It exists because a safety net nobody has ever dropped
into is not a demonstrated safety net.

### The human gate, end to end

The gate is two signatures: a billing clerk confirming the paper trail, and the
ordering clinician signing the clinical argument. Nothing transmits until both
are present on the same draft.

Set a state path so separate processes share the same cases, then drive it:

```bash
export OVERTURN_LOCAL_STATE_PATH=./local_state/store.json

uv run python scripts/run_pipeline.py CASE-001
uv run python scripts/casectl.py list
uv run python scripts/casectl.py show CASE-001 --letter --trail
uv run python scripts/casectl.py approve CASE-001 --by clerk@clinic.example
uv run python scripts/casectl.py cosign CASE-001 --clinician "M. Castellanos" --credential MD
```

The clerk's approval alone reports `Not transmitted — clerk=yes clinician=no`.
The co-sign is what sends it. Either order works; whichever signature lands
second is what transmits.

Then let the payer stay silent and watch the case climb on its own:

```bash
uv run python scripts/casectl.py tick
```

### The human approval interface

```bash
uv run uvicorn services.approval_ui.main:app --port 8080
```

### Measuring retrieval

Retrieval decides which policy the entire case is argued against, so its
thresholds are set from measurement rather than intuition. This prints the score
distribution and **fails** if the correct-policy and no-policy populations
overlap:

```bash
uv run python scripts/calibrate_retrieval.py
```

### Regenerating the patient charts

Optional, and needs a JRE plus Synthea:

```bash
java -jar synthea-with-dependencies.jar -p 8 -s 20260822 \
  --exporter.csv.export=true Massachusetts
uv run python scripts/build_charts.py --synthea ~/synthea/output/csv
```

The seed `20260822` is recorded deliberately — the same seed reproduces the same
eight patients, so the charts in this repository can be regenerated exactly.

## Deploying it

Full detail — prerequisites, verification commands, cost breakdown, and a
troubleshooting table — is in [`docs/RUNBOOK.md`](docs/RUNBOOK.md). This is
just the sequence, in order:

```bash
export PROJECT_ID=your-project

bash infra/enable_apis.sh          # turns on the 16 APIs the fleet needs. free.
bash infra/iam_setup.sh            # creates 8 agent service accounts, least privilege. free.
bash infra/iam_audit.sh            # prints what each identity can actually do. free, read-only.

# Optional, and billed the moment it's created: Sentinel's Model Armor layer.
# Sentinel runs fine without it — it records the layer as skipped, not clean.
bash infra/model_armor_setup.sh    # optional. creates a billable-ish resource.

bash infra/provision.sh            # buckets, Pub/Sub + DLQ, Firestore, uploads the policy corpus. free at demo volume.
bash infra/deploy.sh               # builds the image (Dockerfile, via Cloud Build) once, deploys
                                    # all three Cloud Run services, wires Pub/Sub push and the
                                    # Cloud Scheduler job. pennies (Cloud Build minutes, Artifact
                                    # Registry storage); the real cost is Vertex AI calls per case.
```

`infra/deploy.sh` is also what you re-run for every subsequent code change.
`infra/provision.sh` and `infra/deploy.sh` must run after `iam_setup.sh` —
both grant IAM bindings on resources they create, targeting service accounts
that have to exist first.

Tear down what costs money while idle with `bash infra/teardown.sh` (add
`--delete-data` to also remove the buckets); see the runbook for what is and
isn't deleted.

## Data sources and compliance

**No real patient data is used anywhere in this project, and none was
consulted.**

- **Patient charts.** Demographics, problem lists and longitudinal labs come
  from [Synthea](https://github.com/synthetichealth/synthea), an open-source
  synthetic patient generator. Clinically relevant encounters are authored for
  this project, because no general-purpose generator produces records aligned to
  one fictional payer's numbered criteria. Every record carries a `provenance`
  field saying which it is. See `data/charts/README.md`.
- **MIMIC-III and MIMIC-IV were deliberately not used.** The PhysioNet
  credentialed data use agreement prohibits redistribution, and this project's
  demonstration video is public. Using it would breach that agreement regardless
  of intent.
- **Payer policies.** "Northbeck Health Plan" is a fictional insurer invented for
  this project. No real insurer's name, logo, or policy text appears anywhere.
  The documents are authored, modelled on the *structure* that real payers
  publish openly — a scope statement, numbered coverage criteria, documentation
  requirements, exclusions. See `data/policies/README.md`.
- **No medical advice.** Stated above and enforced in code.

## Prior work and disclosures

All code in this repository was written during the hackathon submission period
beginning 2026-08-22. No pre-existing project, template, or private codebase was
incorporated.

Standard development tooling was used and requires no disclosure under the
contest rules: Google ADK, the Google Gen AI SDK, FastAPI, Pydantic, pytest,
Ruff, `uv`, and AI coding assistants. Third-party components that ship as data
or binaries rather than as dependencies are called out above: Synthea
(Apache 2.0) for the patient base, and Eclipse Temurin as the JRE used to run it.

## Documentation

- [`docs/PLATFORM_PROBE.md`](docs/PLATFORM_PROBE.md) — what was actually
  available on the project, resolved on day one before anything was designed
- [`docs/MODEL_CHOICES.md`](docs/MODEL_CHOICES.md) — which agent uses which
  model, and the constraint that ruled out every Pro-tier option
- [`docs/SCREENING_LAYERS.md`](docs/SCREENING_LAYERS.md) — what each of
  Sentinel's three detection layers actually caught when measured against a
  poisoned letter, including the one that missed
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — how the live Cloud Run deployment
  above was built, and the non-obvious failures that cost real time getting
  there
