#!/usr/bin/env bash
set -euo pipefail

readonly RESULT_FILE=/var/lib/coding-jobs/bootstrap-audit.json
readonly PROMPT_FILE=/opt/agy-bootstrap/prompt.txt
readonly SCHEMA_FILE=/opt/agy-bootstrap/result-schema.json
readonly MAX_TURNS=3
readonly CONTINUE_PROMPT='Continue the existing repository audit. Wait for any delegated read-only research to complete, synthesize the findings, and return the final implementation-ready result matching the required JSON schema. Do not delegate further, do not invoke commands, and do not modify files.'

run_turn() {
  local prompt="$1"
  local conversation_id="${2:-}"
  local output_file="$3"
  local args=(
    -p "${prompt}"
    --mode=plan
    --output-format json
    --json-schema "${SCHEMA_FILE}"
    --sandbox
    --print-timeout 20m
  )

  if [[ -n "${conversation_id}" ]]; then
    args+=(--conversation "${conversation_id}")
  fi

  agy "${args[@]}" > "${output_file}"
}

for turn in $(seq 1 "${MAX_TURNS}"); do
  if [[ -s "${RESULT_FILE}" ]] && jq -e '.structured_output != null' "${RESULT_FILE}" >/dev/null; then
    jq '.structured_output' "${RESULT_FILE}"
    exit 0
  fi

  conversation_id=""
  prompt="$(cat "${PROMPT_FILE}")"
  if [[ -s "${RESULT_FILE}" ]]; then
    conversation_id="$(jq -r '.conversation_id // empty' "${RESULT_FILE}")"
    if [[ -n "${conversation_id}" ]]; then
      prompt="${CONTINUE_PROMPT}"
    fi
  fi

  turn_file="${RESULT_FILE}.turn-${turn}.tmp"
  run_turn "${prompt}" "${conversation_id}" "${turn_file}"
  mv "${turn_file}" "${RESULT_FILE}"

  if jq -e '.structured_output != null' "${RESULT_FILE}" >/dev/null; then
    jq '.structured_output' "${RESULT_FILE}"
    exit 0
  fi

  sleep 10
done

jq '{status,error,conversation_id,response_preview:((.response // "")[0:500])}' "${RESULT_FILE}" >&2
printf 'Agy did not produce structured output after %s bounded turns.\n' "${MAX_TURNS}" >&2
exit 2
