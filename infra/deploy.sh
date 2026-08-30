#!/usr/bin/env bash
# Build the image once, deploy the three Cloud Run surfaces from it, and wire
# them to the Pub/Sub subscription and Cloud Scheduler job that drive them.
#
# Prerequisite order: enable_apis.sh, iam_setup.sh, provision.sh, then this.
# Safe to re-run — `gcloud run deploy` always rolls a new revision of the same
# service, and every binding/job step below checks before it writes.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[[ -n "${PROJECT_ID}" && "${PROJECT_ID}" != "(unset)" ]] || {
  echo "PROJECT_ID is not set and no gcloud default project is configured." >&2
  exit 1
}

# Same names as iam_setup.sh / provision.sh — do not let these three scripts
# compute a bucket, topic, or region name three different ways.
REGION="${REGION:-us-central1}"
INTAKE_BUCKET="${INTAKE_BUCKET:-${PROJECT_ID}-intake}"
POLICY_BUCKET="${POLICY_BUCKET:-${PROJECT_ID}-policies}"
INTAKE_TOPIC="${INTAKE_TOPIC:-overturn-denial-received}"
DLQ_TOPIC="${DLQ_TOPIC:-overturn-dead-letter}"
SUBSCRIPTION="${SUBSCRIPTION:-overturn-ingest}"

AR_REPO="${AR_REPO:-overturn}"
IMAGE_TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/overturn:${IMAGE_TAG}"

# Cost-safety defaults: every service scales to zero, and the caps below are
# what a demo needs, not what a production payer-facing system would run.
# Override any of these on the command line, e.g. MAX_INGEST=10 bash deploy.sh
MEMORY="${MEMORY:-512Mi}"
CPU="${CPU:-1}"
MAX_INGEST="${MAX_INGEST:-3}"
MAX_APPROVAL="${MAX_APPROVAL:-3}"
MAX_SCHEDULER="${MAX_SCHEDULER:-1}"

# Assumption, because services/ingest_handler and services/scheduler_job were
# being built in parallel with this script and their exact routes were not
# available: the Pub/Sub push endpoint is the ingest service's root path, and
# the scheduler job's tick endpoint is POST /tick. Both are one-line fixes
# below (INGEST_PUSH_PATH, the --uri in the scheduler job) if the real
# handlers land on different routes.
# Must match the route in services/ingest_handler/app.py. A wrong value here
# fails silently in the worst possible way: the deploy succeeds, Cloud Run
# answers the health check, and every denial letter posts to a path that 404s.
# Nothing is broken enough to notice until nothing has been processed for a day.
INGEST_PUSH_PATH="${INGEST_PUSH_PATH:-/pubsub/push}"
SCHEDULER_CRON="${SCHEDULER_CRON:-*/5 * * * *}"

# Empty by default: Sentinel records the Model Armor layer as deliberately
# skipped rather than silently clean when this is unset (see
# core/config.py:model_armor_template). Set it only after running
# infra/model_armor_setup.sh, which creates the template under this same id.
# Accept either spelling. The runtime reads OVERTURN_MODEL_ARMOR_TEMPLATE, so
# that is the name anyone will have in their shell, and silently ignoring it
# here deploys a service that reports the screening layer as "skipped" while the
# operator believes they enabled it.
MODEL_ARMOR_TEMPLATE="${MODEL_ARMOR_TEMPLATE:-${OVERTURN_MODEL_ARMOR_TEMPLATE:-}}"

ORCHESTRATOR_SA="overturn-orchestrator@${PROJECT_ID}.iam.gserviceaccount.com"
LIFECYCLE_SA="overturn-lifecycle@${PROJECT_ID}.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

echo "== Deploying Overturn to ${PROJECT_ID} (${REGION}) =="
echo

# --- Artifact Registry ---------------------------------------------------------
if ! gcloud artifacts repositories describe "${AR_REPO}" \
     --project="${PROJECT_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${AR_REPO}" \
    --project="${PROJECT_ID}" --location="${REGION}" \
    --repository-format=docker \
    --description="Overturn service images" >/dev/null
  echo "created Artifact Registry repo: ${AR_REPO}"
else
  echo "Artifact Registry repo exists: ${AR_REPO}"
fi

# --- Build once ------------------------------------------------------------
echo
echo "-- Building ${IMAGE} --"
gcloud builds submit "$(dirname "$0")/.." --project="${PROJECT_ID}" --tag="${IMAGE}"

# Common env vars every surface needs to run against real Cloud instead of the
# local offline backend. OVERTURN_SERVICE is set per-service below, selecting
# which ASGI app the Dockerfile's entrypoint starts.
COMMON_ENV="OVERTURN_PROJECT_ID=${PROJECT_ID}"
COMMON_ENV+=",OVERTURN_LOCATION=${REGION}"
COMMON_ENV+=",OVERTURN_RUNTIME_MODE=cloud,GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${MODEL_LOCATION:-global}"
COMMON_ENV+=",OVERTURN_LLM_BACKEND=adk"
COMMON_ENV+=",OVERTURN_INTAKE_BUCKET=${INTAKE_BUCKET}"
COMMON_ENV+=",OVERTURN_POLICY_BUCKET=${POLICY_BUCKET}"
COMMON_ENV+=",OVERTURN_INTAKE_TOPIC=${INTAKE_TOPIC}"
COMMON_ENV+=",OVERTURN_DEAD_LETTER_TOPIC=${DLQ_TOPIC}"
if [[ -n "${MODEL_ARMOR_TEMPLATE}" ]]; then
  COMMON_ENV+=",OVERTURN_MODEL_ARMOR_TEMPLATE=${MODEL_ARMOR_TEMPLATE}"
fi

# The login, carried through the deploy rather than restored after it.
#
# `--set-env-vars` replaces the container's whole environment. It does not
# merge. So every run of this script used to strip OVERTURN_UI_PASSWORD and
# OVERTURN_UI_SECRET off the approval service -- the two variables that put a
# door in front of the queue -- and the app came back up serving patient names,
# case ids and drafted appeals to anyone with the URL. open_public.sh put them
# back, but only if somebody remembered to run it, and the window in between was
# a public medical-looking record. That happened once, for real, on 2026-08-30.
#
# Read from Secret Manager, the same secret open_public.sh creates and reuses,
# so the session signing key stays stable across revisions. If the secret does
# not exist yet this stays empty.
#
# Correction to an earlier version of this comment: that does NOT make the
# app "refuse to serve the queue." AuthConfig.enabled in services/approval_ui/auth.py
# is False with no password set, which turns off the app-level door entirely --
# every route, including the three that change a case, would be reachable with
# no login at all. What actually keeps a fresh deploy closed is the
# --no-allow-unauthenticated flag below, Cloud Run's own IAM check, which is
# unconditional and does not depend on whether this secret exists.
UI_ENV=""
UI_PASSWORD="${OVERTURN_UI_PASSWORD:-northbeck-appeals-2026}"
if gcloud secrets describe overturn-ui-secret --project="${PROJECT_ID}" >/dev/null 2>&1; then
  UI_SECRET="$(gcloud secrets versions access latest \
                 --secret=overturn-ui-secret --project="${PROJECT_ID}")"
  UI_ENV="OVERTURN_UI_PASSWORD=${UI_PASSWORD},OVERTURN_UI_SECRET=${UI_SECRET}"
else
  echo "WARNING: no overturn-ui-secret found. The approval service will deploy" >&2
  echo "         without a login configured. Run infra/open_public.sh." >&2
fi

deploy_service() {
  local name="$1" sa="$2" max_instances="$3" allow_unauth="$4" service_flag="$5"
  # Sixth argument is optional and defaults to scale-to-zero, which is right for
  # everything a machine calls. Only the surface a person opens sets it to 1.
  local min_instances="${6:-0}"
  local auth_flag="--no-allow-unauthenticated"
  if [[ "${allow_unauth}" == "true" ]]; then
    auth_flag="--allow-unauthenticated"
  fi

  # The approval surface carries the login with it. Appended here rather than
  # applied afterwards, because "afterwards" is a window in which the queue is
  # open to the internet.
  local env_vars="${COMMON_ENV},OVERTURN_SERVICE=${service_flag}"
  if [[ "${service_flag}" == "approval" && -n "${UI_ENV}" ]]; then
    env_vars+=",${UI_ENV}"
  fi

  gcloud run deploy "${name}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${IMAGE}" \
    --service-account="${sa}" \
    --set-env-vars="${env_vars}" \
    --min-instances="${min_instances}" \
    --max-instances="${max_instances}" \
    --memory="${MEMORY}" \
    --cpu="${CPU}" \
    ${auth_flag} \
    --quiet
}

echo
echo "-- overturn-ingest (private, orchestrator identity) --"
deploy_service overturn-ingest "${ORCHESTRATOR_SA}" "${MAX_INGEST}" false ingest

echo
# The service named plainly `overturn` because it serves the whole product: the
# public site at / and the review queue at /queue, one address, one deployment.
# The site pages are public (they describe the system and hold no case data).
# Reading a case is public too -- services/approval_ui/auth.py's
# require_session middleware only gates the three routes that change one
# (approve, reject, co-sign); GET and HEAD bypass the app-level login
# entirely. This comment used to claim "everything that renders a case sits
# behind the login," which stopped being true once reading was opened up.
#
# Two gates stack here regardless. This script leaves Cloud Run's IAM check
# on, which returns 403 in a browser for the whole service, reading included.
# `infra/open_public.sh` is the deliberate, separate act that removes that
# IAM check and leaves the app-level password as the only remaining gate --
# and even that only ever gated the three write routes -- so publishing is
# something someone chose, not a side effect of deploying.
#
# min-instances is 1 rather than 0: this is the first thing a visitor sees, and
# a cold start on the landing page is a bad way to open.
echo "-- overturn (site + review queue, IAM-gated, orchestrator identity) --"
deploy_service overturn "${ORCHESTRATOR_SA}" "${MAX_APPROVAL}" false approval 1

echo
echo "-- overturn-scheduler (private, lifecycle identity) --"
deploy_service overturn-scheduler "${LIFECYCLE_SA}" "${MAX_SCHEDULER}" false scheduler

INGEST_URL="$(gcloud run services describe overturn-ingest --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
APPROVAL_URL="$(gcloud run services describe overturn --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
SCHEDULER_URL="$(gcloud run services describe overturn-scheduler --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"

# Whoever ran this script can reach the approval UI without standing up IAP.
CALLER="$(gcloud config get-value account 2>/dev/null)"
if [[ -n "${CALLER}" ]]; then
  gcloud run services add-iam-policy-binding overturn \
    --project="${PROJECT_ID}" --region="${REGION}" \
    --member="user:${CALLER}" --role="roles/run.invoker" --quiet >/dev/null
fi

# --- Wire Pub/Sub push to overturn-ingest ---------------------------------
echo
echo "-- Wiring Pub/Sub push -> overturn-ingest --"
PUBSUB_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

# Push subscriptions authenticate by minting an OIDC token for the identity
# named in --push-auth-service-account. Pub/Sub's own service agent needs
# permission to mint tokens as that identity — easy to miss, and pushes fail
# silently (well, land in the DLQ) without it.
gcloud iam service-accounts add-iam-policy-binding "${ORCHESTRATOR_SA}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${PUBSUB_SERVICE_AGENT}" \
  --role="roles/iam.serviceAccountTokenCreator" --quiet >/dev/null

gcloud run services add-iam-policy-binding overturn-ingest \
  --project="${PROJECT_ID}" --region="${REGION}" \
  --member="serviceAccount:${ORCHESTRATOR_SA}" --role="roles/run.invoker" --quiet >/dev/null

gcloud pubsub subscriptions update "${SUBSCRIPTION}" \
  --project="${PROJECT_ID}" \
  --push-endpoint="${INGEST_URL}${INGEST_PUSH_PATH}" \
  --push-auth-service-account="${ORCHESTRATOR_SA}" >/dev/null
echo "  ${SUBSCRIPTION} now pushes to ${INGEST_URL}${INGEST_PUSH_PATH}"

# --- Wire Cloud Scheduler -> overturn-scheduler /tick -----------------------
echo
echo "-- Wiring Cloud Scheduler -> overturn-scheduler --"
CLOUDSCHEDULER_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"

gcloud iam service-accounts add-iam-policy-binding "${LIFECYCLE_SA}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${CLOUDSCHEDULER_SERVICE_AGENT}" \
  --role="roles/iam.serviceAccountTokenCreator" --quiet >/dev/null

gcloud run services add-iam-policy-binding overturn-scheduler \
  --project="${PROJECT_ID}" --region="${REGION}" \
  --member="serviceAccount:${LIFECYCLE_SA}" --role="roles/run.invoker" --quiet >/dev/null

if gcloud scheduler jobs describe overturn-tick --project="${PROJECT_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http overturn-tick \
    --project="${PROJECT_ID}" --location="${REGION}" \
    --schedule="${SCHEDULER_CRON}" \
    --uri="${SCHEDULER_URL}/tick" \
    --http-method=POST \
    --oidc-service-account-email="${LIFECYCLE_SA}" \
    --oidc-token-audience="${SCHEDULER_URL}" >/dev/null
  echo "  updated: Cloud Scheduler job overturn-tick (${SCHEDULER_CRON})"
else
  gcloud scheduler jobs create http overturn-tick \
    --project="${PROJECT_ID}" --location="${REGION}" \
    --schedule="${SCHEDULER_CRON}" \
    --uri="${SCHEDULER_URL}/tick" \
    --http-method=POST \
    --oidc-service-account-email="${LIFECYCLE_SA}" \
    --oidc-token-audience="${SCHEDULER_URL}" >/dev/null
  echo "  created: Cloud Scheduler job overturn-tick (${SCHEDULER_CRON})"
fi

echo
echo "== Done =="
echo "Image:      ${IMAGE}"
echo "Ingest:     ${INGEST_URL}   (private — Pub/Sub push only)"
echo "Approval:   ${APPROVAL_URL}   (IAM-gated — see docs/RUNBOOK.md to open it)"
echo "Scheduler:  ${SCHEDULER_URL}   (private — Cloud Scheduler only)"
echo
echo "These three URLs are the proof-of-cloud-deployment shot for the demo video."
