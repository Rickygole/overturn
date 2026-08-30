# Platform probe — 2026-08-22

Resolved on day one, hour one, before any architecture was committed to. Every
result below came from control-plane metadata calls, which cost nothing.

Project: `overturn-506402` · Region: `us-central1` · Billing: enabled

## GEAP component availability

| GEAP component | Probe | Result | Decision |
|---|---|---|---|
| Agent Runtime | `GET .../reasoningEngines` | `200 {}` | **Available.** Managed runtime is reachable and empty. |
| Memory Bank | Sub-resource of `reasoningEngines` | `200 {}` | **Available** via the same surface. |
| Model Armor | `GET modelarmor.us-central1.rep.googleapis.com/.../templates` | `200 {}` | **Available.** Sentinel gets the real guardrail, not a substitute. |
| Vector search | `GET .../indexes` and Discovery Engine dataStores | `200 {}` | **Available.** Serverless index, no dedicated cluster. |
| Agent Registry | No public REST surface found on this project | not reachable | Implemented on primitives — see below. |
| Agent Gateway | No public REST surface found on this project | not reachable | Implemented on primitives — see below. |
| Agent Identity | Standard IAM service accounts | n/a | IAM per-agent service accounts, one per agent. |

The regional Model Armor endpoint is the one that answers. The global endpoint
`modelarmor.googleapis.com` returns `403 PERMISSION_DENIED` for this project;
that is an endpoint-selection detail, not a missing capability.

## Model catalog

`gemini-3.5-flash` and `gemini-3.7-flash` are both listed **GA** in the
us-central1 catalog, alongside `gemma-4-26b-a4b-it-maas` in public preview.
The hackathon requires Gemini 3.5 or newer, so the target is met by the
catalog. Reachability for inference is confirmed separately by
`scripts/probe_models.py`; see `docs/MODEL_CHOICES.md` for the recorded result
and the reasoning behind which agent uses which model.

An earlier probe run returned `404` for every Gemini 3.x id while returning
`200` for 2.5. That run predated setting a quota project on the local
Application Default Credentials:

```
gcloud auth application-default set-quota-project overturn-506402
```

Worth writing down because the error surfaced as `404 NOT_FOUND` on the model
rather than as a quota or permission error, which points at the wrong problem.

## What this means for the build

Day-one intent was to resolve the GEAP fork toward managed components for the
three that matter most to the track: Agent Runtime for the long-running
Lifecycle agent, Memory Bank for cross-session case context, and Model Armor
for Sentinel. That plan changed for two of the three once the shape of the
problem was clearer — worth recording here so this file does not go on
implying the plan is what got built.

Model Armor is managed, as planned: it is Sentinel's second screening layer.
Agent Runtime and Memory Bank were both reachable — the row above says so —
and both ended up **not used, by choice**. A managed runtime session and a
managed Memory Bank scoped to that session do not fit a case that has to
survive weeks of silence between a submission and a payer's answer; Cloud Run
plus Firestore does. `docs/ARCHITECTURE.md` carries the mapping table and the
reasoning for every row, including the two built on primitives — Agent
Registry and Agent Gateway — because *those* had no reachable surface at all,
which is a different reason and the one this file's table actually shows.
