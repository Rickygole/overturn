#!/usr/bin/env bash
# Create the stateful Google Cloud resources Overturn needs before anything is
# deployed: the two buckets, the Pub/Sub plumbing, the Firestore database, and
# the policy corpus sitting where Retrieval expects to eventually read it from.
#
# Run once per project. Safe to re-run: every step checks for the resource
# first and skips it if it already exists, so a partial failure can just be
# re-run from the top.
#
# Order matters once, on the very first run: enable_apis.sh and iam_setup.sh
# must have already run, because this script grants IAM bindings on resources
# it creates (buckets, topics) and those bindings target service accounts that
# have to exist first.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[[ -n "${PROJECT_ID}" && "${PROJECT_ID}" != "(unset)" ]] || {
  echo "PROJECT_ID is not set and no gcloud default project is configured." >&2
  exit 1
}

# Same defaults and same override variables as iam_setup.sh, on purpose — the
# two scripts must never disagree about what a bucket or topic is called.
REGION="${REGION:-us-central1}"
INTAKE_BUCKET="${INTAKE_BUCKET:-${PROJECT_ID}-intake}"
POLICY_BUCKET="${POLICY_BUCKET:-${PROJECT_ID}-policies}"
INTAKE_TOPIC="${INTAKE_TOPIC:-overturn-denial-received}"
DLQ_TOPIC="${DLQ_TOPIC:-overturn-dead-letter}"
SUBSCRIPTION="${SUBSCRIPTION:-overturn-ingest}"

# Ack deadline is generous on purpose: one message triggers the full pipeline
# (Sentinel through Drafting/Verification, up to max_verification_attempts
# round trips), which is real LLM latency, not a web request. 300s leaves
# headroom without waiting the full 600s ceiling before a retry is considered.
ACK_DEADLINE="${ACK_DEADLINE:-300}"
MAX_DELIVERY_ATTEMPTS="${MAX_DELIVERY_ATTEMPTS:-5}"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
PUBSUB_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
GCS_SERVICE_AGENT="service-${PROJECT_NUMBER}@gs-project-accounts.iam.gserviceaccount.com"

created=()
existed=()

echo "== Provisioning stateful resources on ${PROJECT_ID} (${REGION}) =="
echo

# --- Buckets -----------------------------------------------------------------
create_bucket() {
  local bucket="$1"
  if gcloud storage buckets describe "gs://${bucket}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  exists: gs://${bucket}"
    existed+=("gs://${bucket}")
  else
    gcloud storage buckets create "gs://${bucket}" \
      --project="${PROJECT_ID}" \
      --location="${REGION}" \
      --uniform-bucket-level-access \
      --public-access-prevention >/dev/null
    echo "  created: gs://${bucket}"
    created+=("gs://${bucket}")
  fi
}

echo "-- Cloud Storage --"
create_bucket "${INTAKE_BUCKET}"
create_bucket "${POLICY_BUCKET}"
echo

# --- Pub/Sub -------------------------------------------------------------------
create_topic() {
  local topic="$1"
  if gcloud pubsub topics describe "${topic}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  exists: topic ${topic}"
    existed+=("topic:${topic}")
  else
    gcloud pubsub topics create "${topic}" --project="${PROJECT_ID}" >/dev/null
    echo "  created: topic ${topic}"
    created+=("topic:${topic}")
  fi
}

echo "-- Pub/Sub --"
create_topic "${INTAKE_TOPIC}"
create_topic "${DLQ_TOPIC}"

# The dead-letter path is a separate Google-managed identity, not the
# publishing agent's identity, so it needs its own grants regardless of
# anything iam_setup.sh already gave overturn-orchestrator. Two grants, both
# undocumented gotchas the first time you set up a Pub/Sub dead-letter policy:
#   1. the Pub/Sub service agent must be allowed to publish to the DLQ topic
#   2. the Pub/Sub service agent must be allowed to consume the source
#      subscription, so it can pull the message it is about to dead-letter
gcloud pubsub topics add-iam-policy-binding "${DLQ_TOPIC}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${PUBSUB_SERVICE_AGENT}" \
  --role="roles/pubsub.publisher" --quiet >/dev/null

if gcloud pubsub subscriptions describe "${SUBSCRIPTION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "  exists: subscription ${SUBSCRIPTION}"
  existed+=("subscription:${SUBSCRIPTION}")
  # Update in place so a change to ack deadline or delivery attempts on a
  # re-run actually takes effect instead of being silently ignored.
  gcloud pubsub subscriptions update "${SUBSCRIPTION}" \
    --project="${PROJECT_ID}" \
    --ack-deadline="${ACK_DEADLINE}" \
    --dead-letter-topic="${DLQ_TOPIC}" \
    --max-delivery-attempts="${MAX_DELIVERY_ATTEMPTS}" >/dev/null
else
  # Created as a pull subscription here; infra/deploy.sh converts it to push
  # once the overturn-ingest Cloud Run URL exists, since a push endpoint can't
  # be pointed at a service this script has no business deploying.
  gcloud pubsub subscriptions create "${SUBSCRIPTION}" \
    --project="${PROJECT_ID}" \
    --topic="${INTAKE_TOPIC}" \
    --ack-deadline="${ACK_DEADLINE}" \
    --dead-letter-topic="${DLQ_TOPIC}" \
    --max-delivery-attempts="${MAX_DELIVERY_ATTEMPTS}" >/dev/null
  echo "  created: subscription ${SUBSCRIPTION} (pull for now; infra/deploy.sh switches it to push)"
  created+=("subscription:${SUBSCRIPTION}")
fi

gcloud pubsub subscriptions add-iam-policy-binding "${SUBSCRIPTION}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${PUBSUB_SERVICE_AGENT}" \
  --role="roles/pubsub.subscriber" --quiet >/dev/null
echo

# --- Cloud Storage -> Pub/Sub notification ------------------------------------
echo "-- Bucket notification --"
# The Cloud Storage service agent is created lazily on first use, so granting it
# publish rights fails on a fresh project with "service account does not exist".
# Asking for it is what brings it into being.
GCS_AGENT="$(gcloud storage service-agent --project="${PROJECT_ID}" 2>/dev/null)"
echo "  storage service agent: ${GCS_AGENT}"
if gcloud storage buckets notifications list "gs://${INTAKE_BUCKET}" --project="${PROJECT_ID}" \
     --format="value(topic)" 2>/dev/null | grep -q "${INTAKE_TOPIC}$"; then
  echo "  exists: gs://${INTAKE_BUCKET} -> ${INTAKE_TOPIC}"
  existed+=("notification:${INTAKE_BUCKET}")
else
  # The GCS service agent, not overturn-sentinel or overturn-intake, is the
  # one actually publishing these events, so it needs the grant.
  gcloud pubsub topics add-iam-policy-binding "${INTAKE_TOPIC}" \
    --project="${PROJECT_ID}" \
    --member="serviceAccount:${GCS_SERVICE_AGENT}" \
    --role="roles/pubsub.publisher" --quiet >/dev/null
  gcloud storage buckets notifications create "gs://${INTAKE_BUCKET}" \
    --project="${PROJECT_ID}" \
    --topic="${INTAKE_TOPIC}" \
    --event-types=OBJECT_FINALIZE >/dev/null
  echo "  created: gs://${INTAKE_BUCKET} -> ${INTAKE_TOPIC} (OBJECT_FINALIZE)"
  created+=("notification:${INTAKE_BUCKET}")
fi
echo

# --- Firestore -----------------------------------------------------------------
echo "-- Firestore --"
if gcloud firestore databases describe --database="(default)" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "  exists: Firestore database (default)"
  existed+=("firestore:(default)")
else
  gcloud firestore databases create \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --type=firestore-native >/dev/null
  echo "  created: Firestore database (default), Native mode, ${REGION}"
  created+=("firestore:(default)")
fi
echo

# --- Policy corpus -------------------------------------------------------------
echo "-- Policy corpus --"
POLICY_DIR="$(cd "$(dirname "$0")/.." && pwd)/data/policies"
if compgen -G "${POLICY_DIR}/*.md" >/dev/null; then
  gcloud storage cp "${POLICY_DIR}"/*.md "gs://${POLICY_BUCKET}/" --project="${PROJECT_ID}" >/dev/null
  echo "  uploaded: $(ls "${POLICY_DIR}"/*.md | wc -l | tr -d ' ') file(s) -> gs://${POLICY_BUCKET}/"
else
  echo "  nothing to upload: no *.md files in ${POLICY_DIR}" >&2
fi
echo

# --- Summary ---------------------------------------------------------------
echo "== Done =="
if [[ ${#created[@]} -gt 0 ]]; then
  echo "Created:"
  printf '  %s\n' "${created[@]}"
else
  echo "Created: nothing new (everything already existed)."
fi
if [[ ${#existed[@]} -gt 0 ]]; then
  echo "Already existed:"
  printf '  %s\n' "${existed[@]}"
fi
echo
echo "Next: bash infra/deploy.sh"
