# Operating Overturn on Google Cloud

This is the order of operations for going from an empty (but billing-enabled)
Google Cloud project to three live Cloud Run services, written so that the gap
between "credits arrive" and "system is live" is minutes, not the hour of
improvisation it would otherwise be.

Nothing here is required to run the system at all — see the README for the
fully offline path. This document is only for the moment a real project exists
and the demo video needs a real `.run.app` URL on screen.

## Prerequisites

- A Google Cloud project with billing enabled. `docs/PLATFORM_PROBE.md` records
  the project this was built against (`overturn-506402`, `us-central1`);
  substitute your own.
- The `gcloud` CLI, authenticated: `gcloud auth login` and
  `gcloud auth application-default login`.
- **Set a quota project on your Application Default Credentials.** This bit
  the team once during the platform probe and surfaces as a confusing
  `404 NOT_FOUND` on a model id rather than a permissions error:
  ```bash
  gcloud auth application-default set-quota-project "$PROJECT_ID"
  ```
- No local Docker install is required. `infra/deploy.sh` builds the image with
  Cloud Build, not a local daemon.
- `uv` is only needed for local/offline development (`uv sync`, `uv run
  pytest`), not for any of the deployment scripts below.

## Four things that will waste your afternoon

Each of these produced a symptom that pointed at something other than its
actual cause. Read this before the first real deployment, not after.

**1. Every Gemini 3.x call 404s, even though Model Garden lists it GA.**
Symptom: `404 NOT_FOUND` on `gemini-3.5-flash` (or any 3.x id) from a regional
endpoint — reads exactly like the model doesn't exist, not like a routing
problem, and since the hackathon requires Gemini 3.5 or newer this looks like
a disqualification wearing the costume of a missing feature. Cause: Gemini 3.x
on this project is served only from the **`global`** Vertex AI endpoint; every
regional endpoint 404s regardless of what the catalogue says. Fix: generative
calls go to `OVERTURN_MODEL_LOCATION=global` (already the default in
`core/config.py`); infrastructure stays regional
(`OVERTURN_LOCATION=us-central1`); embeddings stay regional too
(`OVERTURN_EMBEDDING_LOCATION=us-central1` — the global endpoint doesn't carry
`text-embedding-005`, so the client keeps two connections). `infra/deploy.sh`
already sets the ADK-facing `GOOGLE_CLOUD_LOCATION` env var to `global` by
default — don't override it to a region.

**2. Model Armor reports `unavailable` even though it's provisioned.**
Symptom: Sentinel's audit log shows `model_armor:unavailable(HTTPError)` even
though `infra/model_armor_setup.sh` has run and `OVERTURN_MODEL_ARMOR_TEMPLATE`
is set. Cause: `roles/modelarmor.user` was granted to the `overturn-sentinel`
service account — correct on paper, since Sentinel is the agent that calls it
— but nothing runs as `overturn-sentinel` in the deployed system.
`overturn` and `overturn-ingest` run as `overturn-orchestrator`,
which executes every agent in-process, so the grant has to live on the
identity Cloud Run actually uses. Fix: `infra/iam_setup.sh` now grants
`roles/modelarmor.user` to `overturn-orchestrator` directly, alongside the
sentinel grant; re-run it if your project predates this.

**3. The deploy succeeds, the health check passes, and the first real denial
letter 403s.** Symptom: `infra/deploy.sh` finishes clean, `gcloud run services
list` shows `Ready: True` on all three, `/healthz` answers — and the first
letter dropped in the intake bucket dies on `storage.objects.get` with a 403
that looks nothing like a missing IAM grant. Cause: `infra/iam_setup.sh`'s
per-bucket and per-topic grants ran before `infra/provision.sh` created the
buckets and topics, so on a first pass every one of them had nothing to bind
to. Fix: run `iam_setup.sh` **again**, after `provision.sh` — see "One-time
setup" below for the full order. It now prints `SKIPPED ... (bucket does not
exist yet)` instead of silently no-oping, so the second pass is visibly doing
something the first one could not.

**4. Every message lands in the dead-letter topic and nothing looks broken.**
Symptom: Pub/Sub delivers, Cloud Run is healthy, and `overturn-dead-letter`
quietly fills up — no case is ever created, no error is visible anywhere.
Cause: `infra/deploy.sh`'s default `INGEST_PUSH_PATH` is `/`, but
`services/ingest_handler`'s real route is `POST /pubsub/push`. Fix: always
pass `INGEST_PUSH_PATH=/pubsub/push` explicitly on every `deploy.sh` run — it
is not persisted between invocations, so a redeploy that forgets it silently
resets the push endpoint back to the broken default.

## A stale duplicate service exists on this project

`gcloud run services list` shows **four** services, not three: `overturn`,
`overturn-ingest`, `overturn-scheduler` — and `overturn-approval`, which is an
earlier name for the approval surface. It is still deployed, still bound to
`allUsers`, and still serving a two-day-old image at

    https://overturn-approval-kruy6aauaq-uc.a.run.app

That URL was handed to a judge in an earlier version of this project and was
believed removed. It is not. Anything corrected on the live site is still
standing there, which makes it a second address publishing claims this project
has since retracted — the exact failure the "there is exactly one URL" rule in
`docs/SUBMISSION.md` exists to prevent.

Decide deliberately rather than leaving it: either delete it

    gcloud run services delete overturn-approval --region="$REGION" --project="$PROJECT_ID"

or, if it is worth keeping around, at minimum drop its public binding

    gcloud run services remove-iam-policy-binding overturn-approval \
      --region="$REGION" --project="$PROJECT_ID" \
      --member=allUsers --role=roles/run.invoker

`infra/teardown.sh` now names it alongside `overturn` so a teardown cannot
remove the old one and leave the live one billing, which is what it did before.

## One-time setup, in order

Each script is idempotent — re-running any of them after a partial failure
picks up where it left off rather than erroring or duplicating anything.

**The order below has a repeated step, and it is not a typo.** `iam_setup.sh`
grants per-bucket and per-topic IAM bindings on resources that only exist once
`provision.sh` has run, so a first pass has nothing to bind those grants to.
The fix is to run `iam_setup.sh` a second time, after `provision.sh` — see
item 3 in "Four things that will waste your afternoon" above for what it looks
like when this is skipped.

```bash
export PROJECT_ID=your-project-id
export REGION=us-central1          # optional, this is the default everywhere below

bash infra/enable_apis.sh          # turns on the 16 APIs the fleet needs

bash infra/iam_setup.sh            # first pass: creates the 8 agent service accounts and
                                    # every project-level grant. Per-bucket/per-topic grants
                                    # print SKIPPED here — nothing exists yet to bind to.

bash infra/provision.sh            # buckets, Pub/Sub, Firestore, uploads the policy corpus

bash infra/iam_setup.sh            # second pass — REQUIRED. Buckets and topics now exist,
                                    # so this binds the per-resource grants the first pass
                                    # could only skip. Re-running is not for luck.

bash infra/iam_audit.sh            # prints what each identity can actually do — sanity check

# Optional, and billed the moment it's created: Sentinel's Model Armor layer.
# Skip it and Sentinel still runs — it records the layer as deliberately
# skipped rather than silently clean. Run it only when you want that layer on:
bash infra/model_armor_setup.sh

# infra/deploy.sh's default INGEST_PUSH_PATH is "/", a leftover assumption from
# before services/ingest_handler landed. The real route is POST /pubsub/push —
# override it every time, or the push subscription will point at a path that
# 404s and everything lands in the dead-letter topic. See "Known gaps" below.
INGEST_PUSH_PATH=/pubsub/push bash infra/deploy.sh   # builds the image once, deploys all three Cloud Run services
```

`infra/deploy.sh` is also what you re-run for every subsequent code change —
it always builds a fresh image and rolls a new revision of each service.
Remember `INGEST_PUSH_PATH=/pubsub/push` on every re-run too; it is not
persisted anywhere between invocations.

If you ran `infra/model_armor_setup.sh`, re-run `infra/deploy.sh` with
`MODEL_ARMOR_TEMPLATE=overturn-inbound` (the id the setup script creates) so
the Cloud Run services actually pick it up — and keep `INGEST_PUSH_PATH` set
alongside it, since any `deploy.sh` invocation that omits it resets the push
subscription back to the broken default:

```bash
INGEST_PUSH_PATH=/pubsub/push MODEL_ARMOR_TEMPLATE=overturn-inbound bash infra/deploy.sh
```

## What it costs to run for a day

None of this is an official quote — check the Billing console for the real
number — but the shape of the cost is worth knowing before you turn it on:

| Resource | Idle cost | Cost while used |
|---|---|---|
| Cloud Run (×3, `min-instances=0`) | $0 — scales to zero | Cents per demo run; capped by `max-instances` |
| Cloud Storage (2 buckets) | Negligible — a few MB of letters and policy text | — |
| Pub/Sub | $0 — a demo's message volume is far under the free tier | — |
| Firestore (Native mode) | $0 — a demo's read/write volume is far under the free tier | — |
| Cloud Scheduler (1 job) | $0 — the first 3 jobs per project are free | — |
| Artifact Registry | Pennies per GB-month for one image | — |
| **Vertex AI (Gemini calls via ADK)** | $0 idle | **The line item that actually moves.** Each case through the pipeline is roughly 6–10 model calls (screen, extract, retrieve, map, draft, verify ×2, occasional retries). At Flash pricing, expect well under a dollar for a day of iterating and recording a handful of cases. |

The honest summary: infrastructure is close to free at demo volume because
everything scales to zero; the money is in how many times you run a case
through the model calls while rehearsing.

## Verifying each piece is actually working

Run these after `infra/provision.sh` and `infra/deploy.sh`. Each is the exact
command to point at the thing in question, not a general "check the console."

**Buckets exist and are private:**
```bash
gcloud storage buckets describe "gs://${PROJECT_ID}-intake" --format="value(name,iamConfiguration.publicAccessPrevention)"
gcloud storage buckets describe "gs://${PROJECT_ID}-policies" --format="value(name,iamConfiguration.publicAccessPrevention)"
```
Both should print `enforced` as the second field.

**Policy corpus uploaded:**
```bash
gcloud storage ls "gs://${PROJECT_ID}-policies"
```
Expect the same `.md` files as `data/policies/`.

**Pub/Sub topics, DLQ, and subscription:**
```bash
gcloud pubsub topics list --filter="name:overturn"
gcloud pubsub subscriptions describe overturn-ingest \
  --format="value(pushConfig.pushEndpoint,deadLetterPolicy.deadLetterTopic,ackDeadlineSeconds)"
```
After `infra/deploy.sh` has run, `pushEndpoint` should be the `overturn-ingest`
Cloud Run URL, not empty.

**Firestore is Native mode:**
```bash
gcloud firestore databases describe --database="(default)" --format="value(type,locationId)"
```
Expect `FIRESTORE_NATIVE`.

**The three Cloud Run services are up:**
```bash
gcloud run services list --region="$REGION" --filter="metadata.name:overturn"
```
All three should show `Ready: True`. All three now answer `GET /health` (Cloud Run reserves `/healthz`)
(`overturn` also keeps `/health` as an alias for the same handler).
Health checks stay reachable regardless of the approval UI's login — Cloud
Run probes them before anything else, and a health check behind a login
reports the service unhealthy the moment the login works:
```bash
gcloud run services proxy overturn --region="$REGION" &
curl -s localhost:8080/healthz   # {"status": "ok", "service": "approval_ui"}
```
Everything else on `overturn` — the queue, a case, the clinical
co-sign page — goes through the login page first if `OVERTURN_UI_PASSWORD` is
set on the service. See "The approval UI's login, and what `deploy.sh`
doesn't do for it" in "Known gaps" below for what that requires beyond a plain
`deploy.sh` run.

**IAM bindings are actually scoped the way the README claims:**
```bash
bash infra/iam_audit.sh
```

**Cloud Scheduler is wired up and firing:**
```bash
gcloud scheduler jobs describe overturn-tick --location="$REGION" \
  --format="value(state,httpTarget.uri,schedule)"
gcloud scheduler jobs run overturn-tick --location="$REGION"   # fire it once, on demand
```

## Watching it run: logs and traces

```bash
# Tail all three services at once
gcloud beta run services logs tail overturn-ingest    --region="$REGION"
gcloud beta run services logs tail overturn   --region="$REGION"
gcloud beta run services logs tail overturn-scheduler  --region="$REGION"

# Or read recent logs without tailing
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name=~"overturn-.*"' \
  --limit=50 --format="value(timestamp,resource.labels.service_name,textPayload)"
```

Traces (Sentinel through Verification, per case, with retry attempts as nested
spans — this is the waterfall view the demo video cuts to):
```bash
gcloud trace list --project="$PROJECT_ID" --limit=10
# Or open directly:
echo "https://console.cloud.google.com/traces/list?project=${PROJECT_ID}"
```
Traces only appear once `OVERTURN_RUNTIME_MODE=cloud` is set, which
`infra/deploy.sh` sets by default — see `core/telemetry.py`.

## Running one case through the live system

```bash
gcloud storage cp data/denials/<a-denial-letter> "gs://${PROJECT_ID}-intake/"
```
That upload fires the Cloud Storage notification, which publishes to
`overturn-denial-received`, which pushes to `overturn-ingest`. Watch it with
the log-tail command above, then open the approval UI (see "Verifying" above
for the proxy command) once the case reaches `awaiting_human_approval`.

## Before recording the demo

- [ ] `infra/deploy.sh` has been run against a **clean** state — redeploy right
      before recording so the URLs on screen are from the current build, not a
      stale revision: `INGEST_PUSH_PATH=/pubsub/push bash infra/deploy.sh` (see
      "One-time setup" above for why `INGEST_PUSH_PATH` is not optional).
- [ ] Decide whether to set demo time acceleration for the lifecycle segment,
      then set it and remember to unset it afterward:
      ```bash
      gcloud run services update overturn-scheduler --region="$REGION" \
        --update-env-vars=OVERTURN_DEMO_TIME_ACCELERATION=true,OVERTURN_DEMO_SECONDS_PER_DAY=1
      ```
      This is disclosed on screen in `docs/VIDEO_SCRIPT.md` and in the README —
      keep that disclosure in the recording, don't quietly drop it.
- [ ] If the sabotage demo (a deliberately fabricated citation, to show
      Verification rejecting it) is part of the cut, set
      `OVERTURN_SABOTAGE_DRAFTING=first` or `=always` on `overturn-ingest`
      before recording that case, and unset it after — see
      `core/config.py:sabotage_drafting_on` for what each mode shows.
- [ ] Have these visible on screen at some point, per the Google Cloud
      proof-of-deployment requirement: the three `.run.app` URLs (printed at
      the end of `infra/deploy.sh`), the Cloud Trace waterfall for a real case,
      the Firestore console showing a case document's `status` field changing,
      and the IAM service accounts page showing eight distinct identities.
      `bash infra/iam_audit.sh` on screen is the fastest way to prove the
      permission boundaries are enforced, not just described.
- [ ] Confirm `overturn` is reachable the way you plan to demo it.
      `infra/deploy.sh` deploys it Cloud Run-IAM-gated by default, so either
      proxy it locally (`gcloud run services proxy overturn
      --region="$REGION"`) or grant the presenter's Google account
      `roles/run.invoker` ahead of time (`infra/deploy.sh` already does this
      for whoever ran it). If the plan is to show the public login page
      instead — the state a judge testing the hosted URL gets — that needs the
      manual `--allow-unauthenticated` plus `OVERTURN_UI_PASSWORD` step below
      ("The approval UI's login, and what `deploy.sh` doesn't do for it");
      confirm it is still in effect, since a redeploy since it was last set
      reverts it.
- [ ] Reset `OVERTURN_PAYER_BEHAVIOUR` (see `services/payer_sim.py`) to
      whatever the script needs for that segment — `accept`, `reject`, or a
      simulated timeout — before recording, since it's read fresh on each
      submission.

## Tearing down

The hackathon rules don't require the project to stay live for judging, and
credits are not something to bleed for five weeks after submission:

```bash
bash infra/teardown.sh                 # Cloud Run services, Scheduler job, subscription
bash infra/teardown.sh --delete-data   # the above, plus both buckets and their contents
```

It asks you to type the project ID before deleting anything. Pub/Sub topics
and the Firestore database are left alone either way — they cost nothing idle,
and deleting Firestore data is a one-way door this script doesn't open for you.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `404 NOT_FOUND` on a Gemini model id | ADC has no quota project set | `gcloud auth application-default set-quota-project "$PROJECT_ID"` |
| `infra/deploy.sh` fails at the build step | Cloud Build or Artifact Registry API not enabled | Re-run `infra/enable_apis.sh` |
| Pub/Sub messages piling up in the dead-letter topic | `infra/deploy.sh`'s `INGEST_PUSH_PATH` default (`/`) does not match `services/ingest_handler`'s real route (`POST /pubsub/push`) | Re-run `INGEST_PUSH_PATH=/pubsub/push bash infra/deploy.sh` |
| Push subscription delivers nothing, no error visible | Pub/Sub's service agent lacks `roles/iam.serviceAccountTokenCreator` on the orchestrator SA | `infra/deploy.sh` grants this every run; re-run it |
| Cloud Scheduler job fires but `overturn-scheduler` returns 403 | Scheduler's service agent lacks token-creator on the lifecycle SA, or the lifecycle SA lacks `run.invoker` on the service | `infra/deploy.sh` grants both every run; re-run it |
| `overturn` returns 403 in a browser, no login page at all | It requires Cloud Run IAM auth by design (`infra/deploy.sh` deploys it `--no-allow-unauthenticated`) | `gcloud run services proxy overturn --region="$REGION"`, or grant your account `roles/run.invoker` — or apply the manual public-login step below if the goal is a URL a judge can open with just a password |
| The login page loads, the correct password is accepted, but the next request bounces back to `/login` anyway | `OVERTURN_UI_SECRET` isn't set, so each process (and each Cloud Run instance, if more than one is running) minted its own random signing secret at startup; a session cookie signed by one instance fails validation on another | Set `OVERTURN_UI_SECRET` explicitly to the same value across the service, not just `OVERTURN_UI_PASSWORD` — see the manual step below |
| `infra/provision.sh` errors instead of skipping an existing resource | The existence check for that resource returned a false negative (permissions, wrong project, transient API error) | Re-run with `set -x` to see which `describe` call failed, or check the resource by hand with the matching command in "Verifying" above |
| Sentinel's audit log shows `model_armor:skipped_no_text` or `unavailable(HTTPError)` | `OVERTURN_MODEL_ARMOR_TEMPLATE` isn't set, `infra/model_armor_setup.sh` was never run, or `roles/modelarmor.user` is missing on `overturn-orchestrator` specifically (see "Four things that will waste your afternoon" above) | Run `infra/model_armor_setup.sh`, confirm `overturn-orchestrator` (not just `overturn-sentinel`) holds `roles/modelarmor.user` via `infra/iam_audit.sh`, then redeploy with `INGEST_PUSH_PATH=/pubsub/push MODEL_ARMOR_TEMPLATE=overturn-inbound bash infra/deploy.sh` |

## Known gaps and assumptions, stated plainly

- **`services/ingest_handler` and `services/scheduler_job` now exist, and their
  real routes are only a partial match for what `infra/deploy.sh` assumed.**
  Both expose `services.<name>.main:app` with a `GET /healthz` route, as
  assumed. The scheduler's tick route is `POST /tick`, also as assumed — no
  correction needed there. The ingest handler's push route is
  `POST /pubsub/push`, **not** `/`, which is what `INGEST_PUSH_PATH` defaults
  to in `infra/deploy.sh`. That default was never updated once the handler
  landed, and this repo's remit doesn't include editing `infra/`, so the fix
  lives here instead: always pass `INGEST_PUSH_PATH=/pubsub/push` when running
  `infra/deploy.sh` (see "One-time setup" and the troubleshooting table
  above). Skipping it means the push subscription points at a route that
  doesn't exist and every message ends up in the dead-letter topic.
- **`services/approval_ui` now has a real login (`services/approval_ui/auth.py`,
  `templates/login.html`), and `infra/deploy.sh` has not caught up to it.** A
  shared password (`OVERTURN_UI_PASSWORD`) plus a signed session cookie
  (`OVERTURN_UI_SECRET`) is the door; unset, `AuthConfig.enabled` is `False`
  and there is no door at all — local dev and the test suite deliberately run
  that way. As written today, `infra/deploy.sh` deploys `overturn`
  with `--no-allow-unauthenticated` and never sets `OVERTURN_UI_PASSWORD` or
  `OVERTURN_UI_SECRET` in its environment — so a fresh `deploy.sh` run
  produces a service that is Cloud Run-IAM-gated **and** has no application
  password configured, and the login page nobody outside the project can
  reach is moot. Getting to "a judge opens the `.run.app` URL and types a
  password" — the state the submission checklist assumes — needs one manual
  step this script does not perform:
  ```bash
  gcloud run services update overturn --project="$PROJECT_ID" --region="$REGION" \
    --allow-unauthenticated \
    --update-env-vars=OVERTURN_UI_PASSWORD='northbeck-appeals-2026',OVERTURN_UI_SECRET="$(openssl rand -hex 32)"
  ```
  Setting `OVERTURN_UI_SECRET` explicitly here matters beyond just surviving a
  restart: if it's left unset, each Cloud Run instance mints its own random
  secret at startup, and with `max-instances` above 1 a session issued by one
  instance can fail validation on another (see the troubleshooting table).
  **Re-running `infra/deploy.sh` after this reverts it** — the script always
  redeploys `overturn` with `--no-allow-unauthenticated` and does not
  carry forward env vars it did not set itself, so the public-with-a-password
  state has to be reapplied after every subsequent redeploy until the script
  itself is updated to do this. That fix belongs in `infra/deploy.sh`, which
  is outside the remit of this document.
- **`overturn` and `overturn-ingest` run as `overturn-orchestrator`;
  `overturn-scheduler` runs as `overturn-lifecycle`.** `infra/agents.env` names
  eight agent identities, not eleven, so the three Cloud Run surfaces share
  identities with the agent whose job most matches what they do: the
  orchestrator's declared purpose ("Cloud Run services that route between
  agents") covers ingest and approval; Lifecycle's ("escalates overdue cases
  on a schedule") is scheduler_job's whole purpose. No new service account was
  invented for this.
- **The two-signature gate is complete in the browser, not just in code.** The
  human gate requires two signatures — clerk approval plus a clinician
  co-sign (`CaseRecord.ready_to_submit`) — and `services/approval_ui` exposes
  both: `POST /case/{case_id}/approve` for the clerk (`/case/{case_id}`) and
  `POST /case/{case_id}/cosign` for the ordering clinician
  (`/case/{case_id}/clinical`). Either can be recorded first; whichever lands
  second calls `submit_if_ready`, which reads `CaseRecord.ready_to_submit` and
  transmits. This was a real gap in an earlier state of this repository — a
  case would sit at `approved` with no way to reach `submitted` from the
  deployed web UI — and it no longer is.

## Things that will waste your afternoon

### `/healthz` returns a Google 404 on Cloud Run

**Symptom.** `curl https://<service>.run.app/healthz` returns an HTML page
titled `Error 404 (Not Found)!!1`, while the same route answers locally. The
container logs show no request at all.

**Cause.** Cloud Run's front end reserves `/healthz` and answers it itself. The
request never reaches the container, so nothing in the application can fix it
and nothing in the logs explains it.

**What to use.** `/health`, which all three services answer with
`{"status":"ok","service":...}`. `/healthz` is still registered for anywhere
else this runs, but nothing behind Cloud Run should depend on it.
