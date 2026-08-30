#!/usr/bin/env bash
# Tear down the things that cost money while sitting idle: the three Cloud Run
# services, the Cloud Scheduler job, and the push subscription. Buckets and
# their contents are left alone unless --delete-data is passed explicitly,
# because losing the policy corpus and any cases sitting in Firestore is a
# different kind of decision than losing a scaled-to-zero Cloud Run service.
#
# The hackathon rules do not require the project to stay live for judging, and
# scale-to-zero Cloud Run + an idle Cloud Scheduler job cost close to nothing —
# but "close to nothing" over five weeks is still not "nothing", so run this
# once judging is done.
#
# Safe to re-run: every delete is preceded by an existence check, and deleting
# something already gone is treated as success, not an error.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[[ -n "${PROJECT_ID}" && "${PROJECT_ID}" != "(unset)" ]] || {
  echo "PROJECT_ID is not set and no gcloud default project is configured." >&2
  exit 1
}

REGION="${REGION:-us-central1}"
INTAKE_BUCKET="${INTAKE_BUCKET:-${PROJECT_ID}-intake}"
POLICY_BUCKET="${POLICY_BUCKET:-${PROJECT_ID}-policies}"
SUBSCRIPTION="${SUBSCRIPTION:-overturn-ingest}"

DELETE_DATA=false
for arg in "$@"; do
  case "${arg}" in
    --delete-data) DELETE_DATA=true ;;
    *) echo "Unknown argument: ${arg} (expected: --delete-data)" >&2; exit 1 ;;
  esac
done

echo "About to tear down Overturn on project: ${PROJECT_ID}"
echo "  - Cloud Run services: overturn, overturn-ingest, overturn-approval, overturn-scheduler"
echo "  - Cloud Scheduler job: overturn-tick"
echo "  - Pub/Sub subscription: ${SUBSCRIPTION}"
if [[ "${DELETE_DATA}" == "true" ]]; then
  echo "  - Cloud Storage buckets (and everything in them): gs://${INTAKE_BUCKET}, gs://${POLICY_BUCKET}"
else
  echo "  - Buckets, Pub/Sub topics, and Firestore are left in place (pass --delete-data to also remove the buckets)"
fi
echo
read -r -p "Type the project ID (${PROJECT_ID}) to confirm: " confirmation
if [[ "${confirmation}" != "${PROJECT_ID}" ]]; then
  echo "Confirmation did not match. Nothing was deleted." >&2
  exit 1
fi
echo

echo "-- Cloud Run services --"
# `overturn` is the approval surface the deploy actually creates and the one the
# submission URL points at; `overturn-approval` is an earlier name for the same
# thing that was left running on the project. Both are listed because tearing
# down only the old name leaves the live, billing, publicly-invokable service
# untouched -- which is exactly what this script did before.
for svc in overturn overturn-ingest overturn-approval overturn-scheduler; do
  if gcloud run services describe "${svc}" --project="${PROJECT_ID}" --region="${REGION}" >/dev/null 2>&1; then
    gcloud run services delete "${svc}" --project="${PROJECT_ID}" --region="${REGION}" --quiet >/dev/null
    echo "  deleted: ${svc}"
  else
    echo "  already gone: ${svc}"
  fi
done

echo
echo "-- Cloud Scheduler --"
if gcloud scheduler jobs describe overturn-tick --project="${PROJECT_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs delete overturn-tick --project="${PROJECT_ID}" --location="${REGION}" --quiet >/dev/null
  echo "  deleted: overturn-tick"
else
  echo "  already gone: overturn-tick"
fi

echo
echo "-- Pub/Sub subscription --"
if gcloud pubsub subscriptions describe "${SUBSCRIPTION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud pubsub subscriptions delete "${SUBSCRIPTION}" --project="${PROJECT_ID}" --quiet >/dev/null
  echo "  deleted: ${SUBSCRIPTION}"
else
  echo "  already gone: ${SUBSCRIPTION}"
fi
# Topics are intentionally not deleted here — they cost nothing idle, and
# recreating a subscription that points at data flowing into a bucket that
# still exists is more useful than a clean slate. Delete them by hand with
# `gcloud pubsub topics delete` if you really want the project empty.

if [[ "${DELETE_DATA}" == "true" ]]; then
  echo
  echo "-- Cloud Storage (--delete-data) --"
  for bucket in "${INTAKE_BUCKET}" "${POLICY_BUCKET}"; do
    if gcloud storage buckets describe "gs://${bucket}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
      gcloud storage rm --recursive "gs://${bucket}" --quiet >/dev/null 2>&1 || true
      gcloud storage buckets delete "gs://${bucket}" --project="${PROJECT_ID}" --quiet >/dev/null
      echo "  deleted: gs://${bucket}"
    else
      echo "  already gone: gs://${bucket}"
    fi
  done
fi

echo
echo "== Done =="
echo "Firestore data and Pub/Sub topics were left in place."
[[ "${DELETE_DATA}" == "true" ]] || echo "Buckets were left in place. Re-run with --delete-data to remove them."
