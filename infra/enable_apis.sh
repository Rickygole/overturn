#!/usr/bin/env bash
# Enable every Google Cloud API the Overturn fleet needs.
#
# Safe to re-run: enabling an already-enabled service is a no-op.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[[ -n "${PROJECT_ID}" && "${PROJECT_ID}" != "(unset)" ]] || {
  echo "PROJECT_ID is not set and no gcloud default project is configured." >&2
  exit 1
}

APIS=(
  aiplatform.googleapis.com          # Vertex AI: Gemini, Gemma, embeddings
  run.googleapis.com                 # Cloud Run services and jobs
  cloudscheduler.googleapis.com      # Wakes the Lifecycle job
  pubsub.googleapis.com              # Intake fan-out, at-least-once delivery
  firestore.googleapis.com           # Durable case state
  storage.googleapis.com             # Denial letters and policy corpus
  cloudtrace.googleapis.com          # End-to-end reasoning-chain traces
  secretmanager.googleapis.com       # Payer endpoint credentials
  modelarmor.googleapis.com          # Inline prompt-injection guardrails
  discoveryengine.googleapis.com     # Serverless vector search over the policy corpus
  cloudbuild.googleapis.com          # Container builds for Cloud Run
  artifactregistry.googleapis.com    # Container images
  logging.googleapis.com
  monitoring.googleapis.com
  iam.googleapis.com
  eventarc.googleapis.com
)

echo "Enabling ${#APIS[@]} APIs on ${PROJECT_ID}. This takes a couple of minutes."
gcloud services enable "${APIS[@]}" --project="${PROJECT_ID}"

echo
echo "Enabled:"
gcloud services list --enabled --project="${PROJECT_ID}" \
  --format="value(config.name)" | sort | sed 's/^/  /'
