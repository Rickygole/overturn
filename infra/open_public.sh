#!/usr/bin/env bash
# Open the approval interface to the public internet, behind its own login.
#
# Separate from deploy.sh and deliberately a conscious act: this is the one
# command in the repository that makes something reachable by anyone. The
# contest asks for a hosted URL a judge can open, and everything behind the
# login is synthetic — an invented payer, generated patients — but publishing a
# service should still be a thing someone chose to do rather than a side effect
# of a deploy.
#
# Two gates exist and only one is being removed here. Cloud Run's IAM check is
# what currently returns 403 in a browser; the application password stays.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-overturn}"
UI_PASSWORD="${OVERTURN_UI_PASSWORD:-northbeck-appeals-2026}"

[[ -n "${PROJECT_ID}" && "${PROJECT_ID}" != "(unset)" ]] || {
  echo "PROJECT_ID is not set." >&2; exit 1
}

# The session signing key has to be stable across instances. Generated per
# process, a session minted by one instance is rejected by the next, and the
# login appears to take the password and then refuse to let you in.
secret_name="overturn-ui-secret"
if gcloud secrets describe "${secret_name}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "Reusing existing signing secret."
else
  echo "Creating a signing secret."
  python3 -c "import secrets; print(secrets.token_hex(32))" \
    | gcloud secrets create "${secret_name}" --data-file=- --project="${PROJECT_ID}" >/dev/null
fi
UI_SECRET="$(gcloud secrets versions access latest --secret="${secret_name}" --project="${PROJECT_ID}")"

echo
echo "Configuring the login on ${SERVICE}..."
gcloud run services update "${SERVICE}" \
  --project="${PROJECT_ID}" --region="${REGION}" \
  --update-env-vars="OVERTURN_UI_PASSWORD=${UI_PASSWORD},OVERTURN_UI_SECRET=${UI_SECRET}" \
  --quiet >/dev/null

echo "Removing the Cloud Run IAM gate (the app password stays)..."
gcloud run services add-iam-policy-binding "${SERVICE}" \
  --project="${PROJECT_ID}" --region="${REGION}" \
  --member="allUsers" --role="roles/run.invoker" --quiet >/dev/null

URL="$(gcloud run services describe "${SERVICE}" \
        --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"

echo
echo "== Open =="
echo "  ${URL}"
echo "  password: ${UI_PASSWORD}"
echo
echo "Only ${SERVICE} was opened. overturn-ingest and overturn-scheduler remain"
echo "private, invokable solely by Pub/Sub and Cloud Scheduler."
echo
echo "To close it again:"
echo "  gcloud run services remove-iam-policy-binding ${SERVICE} \\"
echo "    --project=${PROJECT_ID} --region=${REGION} \\"
echo "    --member=allUsers --role=roles/run.invoker"
