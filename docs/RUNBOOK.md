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

## One-time setup, in order

Each script is idempotent — re-running any of them after a partial failure
picks up where it left off rather than erroring or duplicating anything.

```bash
export PROJECT_ID=your-project-id
export REGION=us-central1          # optional, this is the default everywhere below

bash infra/enable_apis.sh          # turns on the 16 APIs the fleet needs
bash infra/iam_setup.sh            # creates the 8 agent service accounts, least privilege
bash infra/iam_audit.sh            # prints what each identity can actually do — sanity check

# Optional, and billed the moment it's created: Sentinel's Model Armor layer.
# Skip it and Sentinel still runs — it records the layer as deliberately
# skipped rather than silently clean. Run it only when you want that layer on:
bash infra/model_armor_setup.sh

bash infra/provision.sh            # buckets, Pub/Sub, Firestore, uploads the policy corpus
bash infra/deploy.sh               # builds the image once, deploys all three Cloud Run services
```

`infra/deploy.sh` is also what you re-run for every subsequent code change —
it always builds a fresh image and rolls a new revision of each service.

If you ran `infra/model_armor_setup.sh`, re-run `infra/deploy.sh` with
`MODEL_ARMOR_TEMPLATE=overturn-inbound` (the id the setup script creates) so
the Cloud Run services actually pick it up:

```bash
MODEL_ARMOR_TEMPLATE=overturn-inbound bash infra/deploy.sh
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
All three should show `Ready: True`. `overturn-approval`'s `/health` endpoint
and the other two services' `/healthz` do not require auth to hit directly
from inside the project:
```bash
gcloud run services proxy overturn-approval --region="$REGION" &
curl -s localhost:8080/health   # {"status": "ok", "service": "approval_ui"}
```

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
gcloud beta run services logs tail overturn-approval   --region="$REGION"
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
      stale revision.
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
- [ ] Confirm `overturn-approval` is reachable the way you plan to demo it — by
      default it requires Cloud Run IAM auth (see "A note on the approval UI's
      authentication" below), so either proxy it locally
      (`gcloud run services proxy overturn-approval --region="$REGION"`) or
      grant the presenter's Google account `roles/run.invoker` ahead of time
      (`infra/deploy.sh` already does this for whoever ran it).
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
| Pub/Sub messages piling up in the dead-letter topic | `overturn-ingest`'s route doesn't match `INGEST_PUSH_PATH` (default `/`) | Check `services/ingest_handler`'s actual push route, then re-run `INGEST_PUSH_PATH=/whatever bash infra/deploy.sh` |
| Push subscription delivers nothing, no error visible | Pub/Sub's service agent lacks `roles/iam.serviceAccountTokenCreator` on the orchestrator SA | `infra/deploy.sh` grants this every run; re-run it |
| Cloud Scheduler job fires but `overturn-scheduler` returns 403 | Scheduler's service agent lacks token-creator on the lifecycle SA, or the lifecycle SA lacks `run.invoker` on the service | `infra/deploy.sh` grants both every run; re-run it |
| `overturn-approval` returns 403 in a browser | It requires Cloud Run IAM auth by design (see below) | `gcloud run services proxy overturn-approval --region="$REGION"`, or grant your account `roles/run.invoker` |
| `infra/provision.sh` errors instead of skipping an existing resource | The existence check for that resource returned a false negative (permissions, wrong project, transient API error) | Re-run with `set -x` to see which `describe` call failed, or check the resource by hand with the matching command in "Verifying" above |
| Sentinel's audit log shows `model_armor:skipped_no_text` or `unavailable` | `OVERTURN_MODEL_ARMOR_TEMPLATE` isn't set, or `infra/model_armor_setup.sh` was never run | Run `infra/model_armor_setup.sh`, then redeploy with `MODEL_ARMOR_TEMPLATE=overturn-inbound bash infra/deploy.sh` |

## Known gaps and assumptions, stated plainly

- **`services/ingest_handler` and `services/scheduler_job` were being built in
  parallel with these scripts.** Both were assumed to expose
  `services.<name>.main:app` with a `/healthz` route, per the brief. The
  Pub/Sub push path (`INGEST_PUSH_PATH`, default `/`) and the scheduler's tick
  route (`/tick`) are the two places to correct if the real handlers land
  differently — both are a one-line rerun, not a redesign.
- **`overturn-approval`'s "public but authenticated" is Cloud Run's own IAM
  check, not a login page.** There is no auth code in `services/approval_ui`
  today. The service gets a real, internet-resolvable `.run.app` URL (the
  "publicly reachable" half), and Cloud Run rejects any request without a
  valid identity token for a principal holding `roles/run.invoker` on it (the
  "behind authentication" half). That is meaningfully different from an
  Identity-Aware-Proxy login screen, which would need an external load
  balancer and an OAuth consent screen this script does not create. If this
  ever carries real case data past a demo, IAP (or auth code in the app
  itself) is the next step, not this script pretending it's already there.
- **`overturn-approval` and `overturn-ingest` run as `overturn-orchestrator`;
  `overturn-scheduler` runs as `overturn-lifecycle`.** `infra/agents.env` names
  eight agent identities, not eleven, so the three Cloud Run surfaces share
  identities with the agent whose job most matches what they do: the
  orchestrator's declared purpose ("Cloud Run services that route between
  agents") covers ingest and approval; Lifecycle's ("escalates overdue cases
  on a schedule") is scheduler_job's whole purpose. No new service account was
  invented for this.
- **`docs/PLATFORM_PROBE.md` references `docs/ARCHITECTURE.md`, which does not
  exist in this repository yet.** Not something this pass could fix without
  writing content that isn't there — flagged here so it isn't silently lost.
