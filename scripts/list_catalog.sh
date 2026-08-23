#!/usr/bin/env bash
# Enumerate the Gemini and Gemma models this project can see, without calling
# any of them. Pure control-plane metadata, so it costs nothing to run.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
LOCATION="${LOCATION:-us-central1}"
TOKEN="$(gcloud auth print-access-token)"

curl -s \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://${LOCATION}-aiplatform.googleapis.com/v1beta1/publishers/google/models?pageSize=300" \
| python3 -c "
import json, sys
data = json.load(sys.stdin)
if 'error' in data:
    print('error:', data['error'].get('message', '')[:200]); raise SystemExit(1)
rows = []
for m in data.get('publisherModels', []):
    name = m['name'].split('/')[-1]
    if 'gemini' in name or 'gemma' in name:
        rows.append((name, m.get('launchStage', '?'), m.get('versionId', '?')))
for name, stage, version in sorted(rows):
    print(f'{name:36} {stage:16} {version}')
print()
print(len(rows), 'Gemini/Gemma entries visible in ${LOCATION}')
"
