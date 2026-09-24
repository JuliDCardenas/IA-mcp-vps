#!/usr/bin/env bash
set -euo pipefail

readonly RESULT_FILE=/var/lib/coding-jobs/bootstrap-audit.json
readonly PROMPT_FILE=/opt/agy-bootstrap/prompt.txt
readonly SCHEMA_FILE=/opt/agy-bootstrap/result-schema.json

agy \
  -p "$(cat "${PROMPT_FILE}")" \
  --output-format json \
  --json-schema "${SCHEMA_FILE}" \
  --sandbox \
  --print-timeout 20m \
  > "${RESULT_FILE}"

jq '.structured_output' "${RESULT_FILE}"
