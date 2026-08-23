# Overturn — pitch and copy

## Tagline (one sentence)

Overturn turns a health insurance denial letter into a policy-cited appeal — verifying every citation against the insurer's own published policy before a human ever sees it, and staying on the case for the weeks it takes the payer to answer.

## Three-sentence version

A clinic billing clerk drops a denial letter into Overturn. It finds the insurer's own published medical policy, checks the patient's chart against that policy criterion by criterion, and drafts an appeal that cites the policy by section number — but nothing reaches a human until a separate verification pass has confirmed every citation is real and every claim is supported by the actual source text. The case doesn't end at submission: its state lives in Firestore for as long as the appeal ladder takes, and a scheduled job wakes the case on its own to escalate if the payer goes silent.

## One-paragraph version

A denial letter is an untrusted document from an outside party, and appealing it correctly is a research task: find the payer's own medical policy, match the chart against it criterion by criterion, and write a letter that cites chapter and section — normally two hours of work a billing clerk rarely has time for, so winnable claims die unappealed. Overturn runs that research task as a pipeline of seven narrow agents — screening, extraction, retrieval, criteria mapping, drafting, verification, and lifecycle — each doing one job with the smallest model that can do it reliably. The part that makes it trustworthy rather than merely fast is Verification: every citation in a draft is checked against the retrieved policy text before a human sees it, and a fabricated or unsupported citation sends the draft back for a rewrite instead of out the door. The part that makes it durable is that no process runs while the case waits — its entire state is a Firestore document, any worker can resume it cold, and a scheduler advances it up a four-rung appeal ladder on its own when a deadline passes with no response. A human approves before anything is ever transmitted.

## README opening section

> ### The problem

A denial letter lands on Denise's desk. She's the billing clerk for a three-provider clinic, and this is the fortieth one this month. Doing this right means finding the insurer's own medical policy, checking the patient's chart against it criterion by criterion, and writing a letter that cites the policy back to them, chapter and section. That's a two-hour job, and Denise has forty other things to bill today, so the honest math is: most of these claims never get appealed, winnable or not.

Overturn does the two-hour job in minutes. It finds the payer's published policy, checks the chart against it, drafts the appeal, verifies its own citations against the real policy text before anyone reads them, and keeps the case alive for the weeks it takes the payer to respond — escalating on its own if they go quiet. A person still approves everything before it goes out.

## Devpost — Features and functionality

Overturn is a pipeline of seven single-purpose agents plus an orchestrator, each running under its own IAM service account, each doing one job with the smallest model that can do it reliably:

- **Sentinel** screens every inbound document before anything downstream sees it. Three independent layers — Model Armor, an open-weights classifier (`gemma-4-26b-a4b-it-maas`), and deterministic rules — because a denial letter is content from an outside party, and any one layer alone has a false-negative rate we can't measure. A document flagged as a prompt injection, tool-poisoning attempt, or suspicious encoding is quarantined; Intake never receives the raw text.
- **Intake** extracts the denied service, the payer's stated reason, and patient identifiers from the document — PDF or scanned fax — using multimodal structured output (`gemini-3.5-flash`).
- **Retrieval** finds the governing section of the insurer's own policy corpus via vector search, falling back to a model-assisted query reformulation only when the first pass scores below a similarity floor.
- **Mapping** checks the patient's chart against every numbered criterion in the retrieved section and returns one of four verdicts per criterion — satisfied, not satisfied, insufficient documentation, or not applicable — each with the chart evidence it relied on. "Insufficient documentation" is a distinct, honest answer from "not satisfied": the system does not guess.
- **Drafting** writes the appeal letter, citing policy sections by their stable identifier (`NBH-<SERVICE>-<NUMBER>-<SECTION>`) rather than paraphrasing them.
- **Verification** is the layer that makes the rest of this trustworthy. It runs three checks on every draft: does every cited identifier exist in the retrieved section set (a deterministic set-membership test, done in Python — a model is never asked a question Python can already answer); does the source text actually support the claim made about it; does the letter assert any clinical fact with no row in the criteria matrix. Any failure rejects the draft, and the specific failure is fed back to Drafting as revision instructions rather than discarded. Drafting and Verification cycle until a draft is clean.
- **A human approval gate** sits between a verified draft and transmission. Nothing leaves the system without a recorded decision from a person.
- **Lifecycle** never runs in the request path. It wakes on a schedule, evaluates which submitted cases are overdue — a pure function of stored state, so a worker that has never seen a case before can evaluate it correctly — and advances the case up a four-rung appeal ladder (first-level appeal → peer-to-peer review → second-level appeal → independent external review), each rung with its own response window and its own next action.

Underneath the agents:

- **The case record is the product.** A `CaseRecord` document in Firestore is the complete state of one denied claim — every agent output, every status transition, every draft and verification attempt. It's what lets any worker resume a case weeks after submission with no memory of having seen it before.
- **An idempotency guard** wraps every action with an effect outside the system (submitting an appeal, filing an escalation), keyed by `(case_id, action_type, attempt)`, so an at-least-once delivery from Pub/Sub or a Cloud Run restart can't file the same appeal twice — with lease expiry for a worker that dies mid-action and a hard error if the same key ever arrives with a different payload.
- **Every agent invocation is a traced span** (OpenTelemetry, exported to Cloud Trace in cloud mode), so one trace shows the entire reasoning chain for a case, including drafting/verification retries nested as child spans under the same parent.
- **Demo mode compresses the payer's response window from days to seconds** so the multi-week lifecycle is observable in a short video. This is disclosed in the README and stated out loud on screen — nowhere is it silent.

## Devpost — Findings and learnings

**A computed field almost broke the entire premise of the product.** `CaseRecord` has `computed_field` properties like `is_overdue` and `draft_attempts` — convenient, derived, and by default included by Pydantic in every `model_dump()`. Every contract in this system also uses `extra="forbid"`, deliberately, so a drifted contract between agents fails loudly at the boundary instead of silently downstream. Those two defaults collide: a naive `to_firestore()` would write the computed keys into the document, and reloading that same document with `model_validate()` would reject it as unexpected fields. Nothing about that failure would show up in a demo, because a happy-path run never writes and reloads a case in the same breath — it would only surface after a real case sat in Firestore for the weeks between submission and a payer's response, which is the one scenario this whole architecture exists to handle. The round-trip test in `tests/test_schemas.py` (`test_no_computed_field_leaks_into_storage`) caught it before it shipped; the fix is a recursive `_strip_computed` helper in `core/schemas/base.py` that every contract's `to_firestore()` runs through. The lesson we wrote down: a case record that can't survive write-then-read isn't a bug, it's an un-resumable case waiting to happen weeks later, so the round-trip test is not a style test — it's a contract test for the async design itself.

**The hackathon's model floor eliminated an entire tier we'd planned around.** The plan going in was "cheap Flash models for extraction and checking, one justified Pro-tier call for the hard reasoning step." The catalog said otherwise: every Gemini 3.x Pro identifier available on this project (`gemini-3-pro-preview`, `gemini-3.1-pro-preview`) is still in public preview and numbered *below* the hackathon's required 3.5 floor, and `gemini-2.5-pro`, while GA, is also below it. There is no Pro-tier model in this catalog that clears the line. So "use Pro for the hard call" became "use the newest GA Flash instead" (`gemini-3.7-flash` for Drafting, the one step where output quality is the actual product) — and we wrote the reasoning down in `docs/MODEL_CHOICES.md` rather than quietly falling back to `gemini-2.5-pro`, which would have been the more capable model and also a disqualifying one.

**A 404 on every Gemini 3.x model id pointed at the wrong problem.** An early model probe returned `404 NOT_FOUND` for every `gemini-3.x` identifier while `gemini-2.5-*` returned `200`, which looks exactly like "these models aren't available on this project yet." The actual cause was an unset quota project on the local Application Default Credentials — `gcloud auth application-default set-quota-project` had never been run. Once set, every 3.x id resolved normally. The takeaway we kept for `docs/PLATFORM_PROBE.md`: a 404 on a model id is not evidence the model doesn't exist, and treating it as a catalog gap instead of an auth/quota misconfiguration would have cost real time chasing the wrong fix.

## LinkedIn post (#AllThingsAgenticHackathon)

A billing clerk gets a health insurance denial letter. Writing a real appeal means finding the insurer's own medical policy, checking the chart against it criterion by criterion, and citing the policy back to them — a two-hour job most clinics don't have time for, so winnable claims die unappealed.

I built Overturn for the Google/Devpost All Things Agentic Hackathon. It reads the denial, retrieves the payer's own published policy, maps the chart against it, drafts an appeal that cites real section numbers, and then verifies every citation against the source text before a human ever sees the draft — a fabricated citation gets the draft rejected and rewritten, not sent. The case doesn't end at submission: state lives in Firestore, and a scheduled job wakes it up to escalate if the payer goes silent for weeks. A human approves before anything is transmitted.

Seven agents, one job each, traced end to end in Cloud Trace. Built on Model Armor, Agent Runtime, and Memory Bank where they fit, and on plain primitives where they didn't exist yet.

#AllThingsAgenticHackathon
