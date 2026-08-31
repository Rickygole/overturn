# Architecture

Seven agents, each with its own service account and its own scope, driving a
denied insurance claim from an inbound letter to an appeal that a human signs —
and then holding that case for weeks while nothing runs.

![Overturn architecture: Cloud Storage and Pub/Sub feed a Cloud Run ingest service; seven agents run behind a per-agent access gateway against seven Firestore collections; Drafting and Verification form a retry loop capped at three attempts; a two-signature human gate blocks transmission; every external effect passes an idempotency guard; after submission no process runs and only the cases document survives until Cloud Scheduler wakes a Cloud Run job that runs the Lifecycle agent.](architecture.svg)

The diagram is a single hand-authored SVG with no external references. It has
its own opaque background rather than a `prefers-color-scheme` block, because an
SVG loaded through `<img>` on GitHub answers to the *operating system's* colour
scheme, not to GitHub's theme toggle — so a media query gets it wrong for anyone
whose two settings disagree. One palette, contrast checked against its own
background, is right in every combination.

---

## One case, end to end

**A letter lands.** A denial arrives as a PDF in `gs://overturn-intake`. Cloud
Storage publishes an object-finalize notification, Pub/Sub pushes it to the
Cloud Run ingest service, and `Pipeline.ingest` derives a case id from the SHA-256
of the document bytes. That derivation is why a redelivered notification finds
the case that already exists instead of opening a second one for the same letter.

**Sentinel screens it first, before anything else reads it.** Three layers, in
order of what they cost and how much they can be talked out of: deterministic
rules, then Model Armor, then a Gemma pass. The layers only produce findings;
`decide_quarantine` in Python decides the consequence, so a model that has been
persuaded to say "this document is fine" does not get a vote on whether the
pipeline halts. On a finding the case goes to `quarantined`, a human is notified,
and nothing downstream ever sees the bytes.

**Intake, Retrieval, Mapping.** Intake turns the letter into typed fields.
Retrieval finds the governing policy and returns *every* criteria-bearing section
of it verbatim — whole policies rather than top-k sections, because a criteria
list truncated by similarity score reads downstream as "the policy did not ask
for that". If nothing in the corpus governs the denial, the case ends at
`declined_no_basis` and says so. Mapping renders a verdict on each criterion
against the chart, with evidence and a locator. If the chart does not document
enough, the case also ends at `declined_no_basis` — the gap is in the
documentation, not the determination, and saying so is more useful than a weak
appeal.

**Drafting and Verification cycle.** Drafting writes from the satisfied criteria
only. Verification checks every citation three ways: does the section id exist in
the retrieved set (Python set membership), does the quoted text support the claim
(model), and is every clinical assertion backed by a row in the matrix (also a
model call -- `verify_assertions` in `agents/verification/agent.py`, not a
lookup). Two of the three checks are model calls; only the first is decided in
Python. This paragraph said "(Python)" for the third one until 29 August, which
mattered because the sentence the trust argument rests on -- a model cannot be
wrong about a question it is never asked -- was attached to a question that is,
in fact, asked.
A rejection returns *specific* findings, which become the next attempt's revision
instructions — telling a writer only that it failed produces the same draft
again. Three attempts is the budget. On the third rejection the case goes to
`needs_human_review` with nothing transmitted.

**The gate.** A verified draft reaches `awaiting_human_approval` and the pipeline
**returns**. The Cloud Run instance dies. Two signatures are required and they
can arrive in either order: a billing clerk confirming the paper trail, and the
ordering clinician co-signing the clinical argument. Whichever lands second calls
`submit_if_ready`, which reads `CaseRecord.ready_to_submit` — never recomputes
it — so there is exactly one definition of what counts as enough. The check runs
at transmission, not at approval, so a case that gains a clinical argument on a
later drafting attempt cannot ride an earlier clerk approval out the door.

**Transmission.** `submit_appeal` goes through the idempotency guard, keyed on
`case_id:action_type:attempt`, and a response deadline is set from the appeal
ladder. Then the case sits.

**The gap.** Between the appeal going out and the payer answering, there is no
process, no open connection, and no object in memory. The state is one Firestore
document. This is the part of the diagram with nothing drawn in it.

**Escalation.** Cloud Scheduler calls `POST /tick` on the scheduler job, which
runs `Pipeline.escalate_overdue`. `find_overdue()` is a pure query over stored
state — `is_overdue` is a function of the document alone, so a worker that has
never seen the case evaluates it correctly. Lifecycle picks the next rung,
constrained by the static `APPEAL_LADDER` table, a new deadline is set, and the
case goes back to waiting. `requires_human` on an escalation decision does *not*
stop the clock; only `halts_ladder`, set when no rung remains, does.

---

## Who can touch what

Generated from `core.gateway.POLICY` — the dict the gateway enforces at runtime.
Agents receive a `GatewayHandle`, never a store client, and `core/state.py` and
`core/audit.py` will not accept a raw client, so there is no second door.

| Agent | Service account | Model | `policy_sections` | `cases` | `audit_events` | `actions` | `quarantine` | `case_memory` | `agent_registry` |
|---|---|---|---|---|---|---|---|---|---|
| **sentinel** | `overturn-sentinel` | `gemma-4-26b-a4b-it-maas` | — | read | append | — | write | — | — |
| **intake** | `overturn-intake` | `gemini-3.5-flash` | — | write | append | — | — | — | — |
| **retrieval** | `overturn-retrieval` | `gemini-3.5-flash` | read | write | append | — | — | — | — |
| **mapping** | `overturn-mapping` | `gemini-3.5-flash` | — | write | append | — | — | — | — |
| **drafting** | `overturn-drafting` | `gemini-3.7-flash` | — | write | append | — | — | — | — |
| **verification** | `overturn-verification` | `gemini-3.5-flash` | read | read | append | — | — | — | — |
| **lifecycle** | `overturn-lifecycle` | `gemini-3.5-flash` | — | write | append | write | — | write | — |
| **orchestrator** | `overturn-orchestrator` | *none* | — | write | append | write | read | write | write |

`write` = create and update. `append` = create only, never modify — every agent
appends to `audit_events` and nobody can rewrite it, which is what makes the
audit log worth reading. `read` = read only. A dash is no grant at all, and
`GatewayHandle.authorize` raises `PolicyViolation` rather than logging and
continuing, because a policy violation is a bug in the fleet.

Regenerate with `uv run python scripts/seed_registry.py`.

### The two absences

**Drafting has no read on `policy_sections`.** It cannot go looking for
supporting material Mapping did not hand it. Every citation in the letter has to
come from the closed world Retrieval returned, which is exactly the set
Verification checks membership against. If Drafting could retrieve, that check
would be checking the wrong thing.

**Verification has no write on `cases`.** It records its verdict by returning it
to the orchestrator, so it structurally cannot edit the draft it is judging.
A reviewer that can revise its subject is not a reviewer.

Neither is a rule someone has to remember. The handle each agent receives will
not perform the call. `tests/test_gateway.py` holds both.

### Where the boundary is real, and where it is code

Buckets, Pub/Sub topics and secrets are scoped per identity by
`infra/iam_setup.sh`, and an agent without the grant cannot reach them —
that boundary is Google Cloud IAM. Firestore has no collection-level IAM, so
collection scoping is deterministic Python in `core/gateway.py` with no model in
the path. Both layers are real. Neither is claimed to be the other.

---

## GEAP component mapping

Resolved on day one from control-plane probes, before any architecture was
committed to. `docs/PLATFORM_PROBE.md` records the raw results.

| GEAP component | What this uses | Why |
|---|---|---|
| **Agent Identity** | Managed — IAM service accounts, one per agent, `infra/iam_setup.sh` | Eight identities with genuinely different grants. `infra/iam_audit.sh` prints what each one can actually do. |
| **Model Armor** | Managed — regional endpoint, `agents/sentinel/armor.py` | It is a purpose-built injection detector maintained by people whose whole job is keeping up with attacks. It runs as Sentinel's *second* layer, never alone: it is a network call, and a screening layer that fails open on a network hiccup is not a screening layer. |
| **Agent Runtime** | Available, deliberately not used — the fleet runs on Cloud Run with ADK in-process (`agents/adk_fleet.py`) | The managed runtime drives a session. A session is precisely the thing that does not survive the multi-week gap this product is built around. Cloud Run plus a Firestore document plus a scheduler does. ADK is still load-bearing: `AdkBackend` executes every generative call through an `LlmAgent` and `Runner`. |
| **Memory Bank** | Available, implemented on Firestore instead — `core/memory.py`, over the `case_memory` collection | The managed surface was reachable (`docs/PLATFORM_PROBE.md` got `200 {}` from it), same as Agent Runtime and Vector search above — this is a choice, not a constraint. A managed Memory Bank is scoped to a session; what this needs to survive is weeks between a submission and a payer's answer, keyed on payer, policy and denial reason code, never on a member. Unit-tested (`tests/test_memory.py`) and wired into `agents/orchestrator/pipeline.py` at the two honest call sites: `try_submit` records that an appeal went out, `_escalate_one` records that the payer's published window lapsed with nothing back (`outcome="no_response"` — the only outcome the simulated payer ever actually produces; nothing is recorded as `overturned` or `upheld` that was never observed). Rendered on every case page. |
| **Agent Registry** | Primitive, with a runtime surface — `core/registry.py` over the `agent_registry` collection, rendered live at `/fleet` | No public REST surface on this project. Entries are *derived* — identity from `infra/agents.env`, model and output contract from the ADK definitions, permissions from `POLICY` — so a catalogue entry cannot drift from the agent it describes. `/fleet` calls `build_catalogue()` at request time rather than rendering anything stored, and needs no session, since it carries no patient data. |
| **Agent Gateway** | Primitive — `core/gateway.py` | No public REST surface on this project, and Firestore has no collection-level IAM regardless. The access matrix is a dict, enforcement is a function, and every datastore consumer routes through it. |
| **Vector search** | Available, not used — `agents/retrieval/lexical.py`, TF-IDF over unigrams plus order-independent word pairs | Honest disclosure: the serverless index is reachable and this does not use it. Six policies and 42 sections is not a vector-search problem, and a lexical scorer is one that can be *calibrated* against the eight cases with the measurement recorded in `docs/MODEL_CHOICES.md`. Three real retrieval bugs were caught that way. A managed index would have hidden all three behind a number nobody could reproduce. |

---

## Failure recovery

### A hallucinated citation

Caught by Verification's existence check before any human sees the draft. The
check is Python set membership against the sections Retrieval actually returned
— a model cannot be wrong about a question it is never asked. The draft is
rejected, the specific finding becomes the next attempt's revision instruction,
and after three attempts the case is flagged for a person rather than sent.
`agents/verification/checks.py`, `core/schemas/verification.py`

### A looping agent

`settings.max_verification_attempts` bounds the draft/verify cycle at three. The
attempt number is an attribute on the trace span, so a loop is *visible* in Cloud
Trace rather than merely survived. Case writes retry at most
`MAX_WRITE_RETRIES = 5` times on optimistic-lock conflict and then raise
`ConcurrentModification` rather than spinning. Nothing in this system retries
without a ceiling.

### A duplicate delivery

Pub/Sub delivers at least once; the second invocation must not file a second
appeal. Every action with an external effect is claimed under
`case_id:action_type:attempt` with a create-only write, and the decision and the
claim happen inside one `atomic_update` — an earlier version read, decided, then
wrote, and two redeliveries racing an expired claim both passed the expiry check
and both executed. A replayed delivery returns the stored result. A racing
worker gets `ActionInFlight` and lets the message redeliver rather than dropping
it. The same key with a different payload is a caller bug and is raised loudly
instead of silently replaying an answer to a different question.

Two counters that mean different things must never share a namespace, which is
why recording a human decision is `RECORD_APPROVAL` and not `NOTIFY_HUMAN`. When
they shared a type, the pipeline numbered attempts by notifications sent and the
approval interface numbered them by draft attempt; both produced
`notify_human:1` with different payloads, the guard correctly reported a payload
mismatch, and a clerk could never approve a case that verified on its first
attempt — which is every clean case.

### A worker that dies mid-action

A claim would otherwise block that action forever, so claims carry a 300-second
lease and an expired claim can be taken over, with the takeover recorded.

There is one case where takeover is *not* safe. `SUBMIT_APPEAL`, `ESCALATE` and
`FILE_EXTERNAL_REVIEW` are in `NOT_SAFELY_REPEATABLE`: if a worker died between
calling the payer and recording that it had, re-running risks a second appeal on
one claim and not running risks none. Neither is safe to choose automatically, so
the case goes to a person with both possibilities stated (`UnsafeToRetry`).
Likewise, an action that previously *failed* raises `ActionPreviouslyFailed`
rather than replaying — the earlier version returned a failed record the same way
it returned a completed one, so a caller retrying after a transient network error
received a successful-looking outcome carrying `result=None` and marked the case
submitted with a payer deadline for an appeal that was never sent.

### A crash mid-pipeline

Nothing is lost, because every stage commits its output to the case document
before the next stage begins, and `_advance` attaches the agent's output and
transitions in one write. Case writes are optimistic-locked on a `revision`
counter, so two workers that both read and both write cannot silently lose an
update — and the lost one might have been the approval. `CaseRepository.load`
reconstructs the full typed `CaseRecord`, not a dictionary, so a worker resuming
a case cold gets the same object the worker that dropped it had.

The audit log is written on the failure path too. An agent that crashed without
leaving a trace is the case an auditor most wants to see.

---

## Reading the diagram

- **Rows are identities, lanes are collections.** Each agent's horizontal line is
  a bus; the chips on it are the grants. A blank cell is a missing wire. The two
  cells outlined in red are the two absences the design turns on.
- **Two elements carry the heaviest weight** — the retry loop and the human gate.
  That is deliberate. They are the two places where this system refuses to act.
- **The empty band near the bottom is not a layout mistake.** It is the product.
  Every lane stops there except `cases`, because between submission and the
  payer's answer that one document is all that exists.

Related: [`PLATFORM_PROBE.md`](PLATFORM_PROBE.md) for what was reachable on the
project, [`MODEL_CHOICES.md`](MODEL_CHOICES.md) for which agent uses which model
and the constraint that ruled out every Pro-tier option.
