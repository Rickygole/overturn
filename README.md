# Overturn

**An agent fleet that appeals wrongful health insurance denials, and keeps
appealing for weeks without being asked twice.**

A clinic billing clerk gets a denial letter. Today they either spend two hours
writing an appeal, or — far more often — they don't, and the claim dies. A large
share of denials that *are* appealed get overturned, which means many of the
ones nobody appeals were winnable. The bottleneck is not judgment. It is that
the labour cost of one appeal exceeds the value of one claim.

Overturn reads the denial letter, finds the insurer's own published medical
policy, checks the patient's chart against that policy criterion by criterion,
drafts an appeal citing the policy by section number, verifies every citation is
real before a human ever sees it, and then holds the case for weeks — escalating
on its own when the payer's response deadline passes in silence.

A human approves before anything is transmitted. That gate is a design decision,
not a limitation.

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
| Typed inter-agent contracts | done — 39 exported models, strict, round-trip tested |
| Per-agent access gateway | done |
| Idempotency guard | done — leases, replay, payload-drift detection |
| Append-only audit log | done |
| OpenTelemetry tracing | done |
| Document store (Firestore + offline) | done |
| Policy corpus | done — 6 policies, 42 sections, 113 citable identifiers |
| Patient charts | done — Synthea base plus authored encounters |
| Seven agents | in progress |
| Cloud deployment | pending |

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

Nothing here requires a cloud project. The offline backend runs the full
pipeline deterministically.

```bash
git clone https://github.com/Rickygole/overturn.git
cd overturn
uv sync
uv run pytest -q
```

Regenerating the patient charts needs Synthea and a JRE:

```bash
java -jar synthea-with-dependencies.jar -p 8 -s 20260822 \
  --exporter.csv.export=true Massachusetts
uv run python scripts/build_charts.py --synthea ~/synthea/output/csv
```

The seed `20260822` is recorded deliberately — the same seed reproduces the same
eight patients, so the charts in this repository can be regenerated exactly.

## Deploying it

```bash
export PROJECT_ID=your-project
bash infra/enable_apis.sh     # enables 16 APIs, idempotent
bash infra/iam_setup.sh       # creates 8 service accounts, least privilege
bash infra/iam_audit.sh       # prints what each identity can actually do
```

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
