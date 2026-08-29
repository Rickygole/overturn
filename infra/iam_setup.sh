#!/usr/bin/env bash
# Create one service account per agent and grant each the narrowest set of
# permissions it can do its job with.
#
# A note on honesty, because this matters for anyone reading the repo:
#
#   Google Cloud IAM is per-resource, not per-Firestore-collection. Buckets,
#   Pub/Sub topics and secrets below really are scoped per identity, and an
#   agent without the grant genuinely cannot read them. Firestore has no
#   collection-level IAM, so collection scoping is enforced deterministically in
#   core/gateway.py, which every agent's datastore access routes through. Both
#   layers are documented in the README permission table; neither is claimed to
#   be the other.
#
# Safe to re-run.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
INTAKE_BUCKET="${INTAKE_BUCKET:-${PROJECT_ID}-intake}"
POLICY_BUCKET="${POLICY_BUCKET:-${PROJECT_ID}-policies}"
INTAKE_TOPIC="${INTAKE_TOPIC:-overturn-denial-received}"
DLQ_TOPIC="${DLQ_TOPIC:-overturn-dead-letter}"

[[ -n "${PROJECT_ID}" && "${PROJECT_ID}" != "(unset)" ]] || {
  echo "PROJECT_ID is not set." >&2; exit 1
}

sa_email() { echo "$1@${PROJECT_ID}.iam.gserviceaccount.com"; }

# Service account creation is rate limited per project per minute, and this
# script creates eight in a row. Without backoff it dies partway through with a
# 429 and leaves the fleet half-built — which then looks like a permissions
# problem rather than a throttle.
create_sa() {
  local id="$1" display="$2" attempt=1
  if gcloud iam service-accounts describe "$(sa_email "$id")" \
       --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  exists: ${id}"
    return 0
  fi

  while (( attempt <= 6 )); do
    if gcloud iam service-accounts create "${id}" \
         --project="${PROJECT_ID}" \
         --display-name="${display}" >/dev/null 2>&1; then
      echo "  created: ${id}"
      return 0
    fi
    # Might have been created by a concurrent run, or we are being throttled.
    if gcloud iam service-accounts describe "$(sa_email "$id")" \
         --project="${PROJECT_ID}" >/dev/null 2>&1; then
      echo "  exists: ${id}"
      return 0
    fi
    echo "  throttled on ${id}, waiting ${attempt}0s (attempt ${attempt}/6)"
    sleep "${attempt}0"
    (( attempt++ ))
  done

  echo "  FAILED to create ${id} after 6 attempts" >&2
  return 1
}

grant_project() {
  local id="$1" role="$2"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:$(sa_email "$id")" \
    --role="${role}" \
    --condition=None \
    --quiet >/dev/null
  echo "  project  ${id} <- ${role}"
}

# Buckets and topics have to exist before they can be granted on, and
# infra/provision.sh is what creates them. Run order is therefore:
#
#   enable_apis.sh -> iam_setup.sh -> provision.sh -> iam_setup.sh -> deploy.sh
#
# The second iam_setup pass is not redundant. On a first run the bucket grants
# here have nothing to bind to, and gcloud reports that as a plain error the
# script used to swallow — so the deploy succeeded, Cloud Run answered its
# health check, and the first denial letter died with a 403 on
# storage.objects.get that looked nothing like a missing IAM grant.
grant_bucket() {
  local id="$1" bucket="$2" role="$3"
  if ! gcloud storage buckets describe "gs://${bucket}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  SKIPPED  ${id} <- ${role} on ${bucket} (bucket does not exist yet;"
    echo "           run infra/provision.sh, then re-run this script)"
    return 0
  fi
  gcloud storage buckets add-iam-policy-binding "gs://${bucket}" \
    --member="serviceAccount:$(sa_email "$id")" \
    --role="${role}" \
    --quiet >/dev/null
  echo "  bucket   ${id} <- ${role} on ${bucket}"
}

grant_topic() {
  local id="$1" topic="$2" role="$3"
  if ! gcloud pubsub topics describe "${topic}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  SKIPPED  ${id} <- ${role} on ${topic} (topic does not exist yet;"
    echo "           run infra/provision.sh, then re-run this script)"
    return 0
  fi
  gcloud pubsub topics add-iam-policy-binding "${topic}" \
    --project="${PROJECT_ID}" \
    --member="serviceAccount:$(sa_email "$id")" \
    --role="${role}" \
    --quiet >/dev/null
  echo "  topic    ${id} <- ${role} on ${topic}"
}

echo "== Creating service accounts on ${PROJECT_ID} =="
while IFS='|' read -r agent sa purpose; do
  [[ -z "${agent}" || "${agent}" == \#* ]] && continue
  create_sa "${sa}" "Overturn ${agent}"
done < "$(dirname "$0")/agents.env"

echo
echo "== Granting least-privilege roles =="

# Every agent writes audit events and emits traces. Nothing else is universal.
for sa in overturn-sentinel overturn-intake overturn-retrieval overturn-mapping \
          overturn-drafting overturn-verification overturn-lifecycle overturn-orchestrator; do
  grant_project "${sa}" "roles/cloudtrace.agent"
  grant_project "${sa}" "roles/logging.logWriter"
  grant_project "${sa}" "roles/datastore.user"
done

# --- Sentinel: screens untrusted input. Reads the intake bucket, calls the
#     guard model and Model Armor. Cannot see the policy corpus or the index.
grant_project overturn-sentinel "roles/aiplatform.user"
grant_project overturn-sentinel "roles/modelarmor.user"
grant_bucket  overturn-sentinel "${INTAKE_BUCKET}" "roles/storage.objectViewer"

# --- Intake: reads the same untrusted bucket, calls Flash. No policy access,
#     because extraction must not be able to consult the answer key.
grant_project overturn-intake "roles/aiplatform.user"
grant_bucket  overturn-intake "${INTAKE_BUCKET}" "roles/storage.objectViewer"

# --- Retrieval: the only agent that can read the policy corpus and query the
#     vector index. It never sees a patient chart.
grant_project overturn-retrieval "roles/aiplatform.user"
grant_project overturn-retrieval "roles/discoveryengine.viewer"
grant_bucket  overturn-retrieval "${POLICY_BUCKET}" "roles/storage.objectViewer"

# --- Mapping: reasons over what it is handed. No storage, no index, no network
#     reach beyond the model.
grant_project overturn-mapping "roles/aiplatform.user"

# --- Drafting: deliberately the most confined agent in the fleet. It has no
#     retrieval access at all, so it cannot go looking for supporting material
#     that Mapping did not give it.
grant_project overturn-drafting "roles/aiplatform.user"

# --- Verification: reads the retrieved sections and the matrix, writes only its
#     verdict. It cannot edit the draft it is judging.
grant_project overturn-verification "roles/aiplatform.user"

# --- Lifecycle: runs on a schedule, publishes notifications, invokes nothing
#     it does not own.
grant_project overturn-lifecycle "roles/aiplatform.user"
grant_topic   overturn-lifecycle "${INTAKE_TOPIC}" "roles/pubsub.publisher"

# --- Orchestrator: the Cloud Run surface. Subscribes to intake, invokes agents,
#     reads secrets for the payer endpoint.
grant_project overturn-orchestrator "roles/aiplatform.user"
# The Cloud Run services run as this identity and execute every agent in-process,
# so it needs what those agents need. Granting Model Armor only to the sentinel
# identity was correct on paper and wrong in fact: the deployed screening layer
# reported "unavailable(HTTPError)" while the sentinel service account, which
# nothing runs as, held the permission.
grant_project overturn-orchestrator "roles/modelarmor.user"
grant_project overturn-orchestrator "roles/run.invoker"
grant_project overturn-orchestrator "roles/secretmanager.secretAccessor"
grant_topic   overturn-orchestrator "${INTAKE_TOPIC}" "roles/pubsub.subscriber"
grant_topic   overturn-orchestrator "${DLQ_TOPIC}" "roles/pubsub.publisher"
grant_bucket  overturn-orchestrator "${INTAKE_BUCKET}" "roles/storage.objectViewer"

echo
echo "Done. Verify with: bash infra/iam_audit.sh"
