#!/usr/bin/env bash
# Print what each agent identity can actually do, straight from the live IAM
# policy. This is the command run on camera to show the boundaries are real and
# not just asserted in a README table.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"

echo "Project-level roles by agent identity on ${PROJECT_ID}"
echo

while IFS='|' read -r agent sa purpose; do
  [[ -z "${agent}" || "${agent}" == \#* ]] && continue
  email="${sa}@${PROJECT_ID}.iam.gserviceaccount.com"
  printf '%-14s %s\n' "${agent}" "${purpose}"
  gcloud projects get-iam-policy "${PROJECT_ID}" \
    --flatten="bindings[].members" \
    --filter="bindings.members:serviceAccount:${email}" \
    --format="value(bindings.role)" 2>/dev/null \
    | sort | sed 's/^/               /' || echo "               (none)"
  echo
done < "$(dirname "$0")/agents.env"

echo "Per-bucket grants (the boundaries IAM enforces directly):"
for bucket in "${PROJECT_ID}-intake" "${PROJECT_ID}-policies"; do
  echo "  gs://${bucket}"
  gcloud storage buckets get-iam-policy "gs://${bucket}" \
    --format="value(bindings.role,bindings.members)" 2>/dev/null \
    | grep overturn | sed 's/^/    /' || echo "    (bucket not created yet)"
done
