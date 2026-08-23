#!/usr/bin/env bash
# Create the Model Armor template backing Sentinel's second screening layer.
#
# Separate from iam_setup.sh because this creates a billable-ish resource and
# should be a deliberate step, and because Sentinel degrades honestly without
# it: the layer is recorded as skipped rather than silently reported clean.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
LOCATION="${LOCATION:-us-central1}"
TEMPLATE_ID="${TEMPLATE_ID:-overturn-inbound}"
ENDPOINT="modelarmor.${LOCATION}.rep.googleapis.com"

echo "Creating Model Armor template ${TEMPLATE_ID} in ${LOCATION}"

# The filters chosen match what a denial letter can plausibly carry. Prompt
# injection and jailbreak are the point. Sensitive data protection catches an
# identifier that has no business in a coverage determination. Malicious URI
# catches an exfiltration target dressed up as a portal link.
curl -sS -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://${ENDPOINT}/v1/projects/${PROJECT_ID}/locations/${LOCATION}/templates?template_id=${TEMPLATE_ID}" \
  -d '{
        "filterConfig": {
          "piAndJailbreakFilterSettings": {
            "filterEnforcement": "ENABLED",
            "confidenceLevel": "LOW_AND_ABOVE"
          },
          "maliciousUriFilterSettings": {
            "filterEnforcement": "ENABLED"
          },
          "sdpSettings": {
            "basicConfig": { "filterEnforcement": "ENABLED" }
          }
        }
      }' | python3 -m json.tool

echo
echo "Set OVERTURN_MODEL_ARMOR_TEMPLATE=${TEMPLATE_ID} on the Cloud Run services."
