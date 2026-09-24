#!/usr/bin/env bash
set -euo pipefail

readonly SETTINGS_DIR="${HOME}/.gemini/antigravity-cli"
readonly SETTINGS_FILE="${SETTINGS_DIR}/settings.json"
readonly TMP_FILE="$(mktemp)"
readonly RULES=(
  'read_file(/workspace/IA-mcp-vps)'
  'read_file(/home/agy/.gemini/antigravity-cli/builtin/skills)'
)

mkdir -p "${SETTINGS_DIR}"

if [[ -s "${SETTINGS_FILE}" ]]; then
  cp "${SETTINGS_FILE}" "${TMP_FILE}"
else
  printf '{}\n' > "${TMP_FILE}"
fi

for rule in "${RULES[@]}"; do
  jq --arg rule "${rule}" \
    '.permissions = (.permissions // {}) | .permissions.allow = (((.permissions.allow // []) + [$rule]) | unique)' \
    "${TMP_FILE}" > "${TMP_FILE}.next"
  mv "${TMP_FILE}.next" "${TMP_FILE}"
done

install -m 0600 "${TMP_FILE}" "${SETTINGS_FILE}"
rm -f "${TMP_FILE}"

for rule in "${RULES[@]}"; do
  jq -e --arg rule "${rule}" '.permissions.allow | index($rule) != null' "${SETTINGS_FILE}" >/dev/null
  printf 'Configured scoped permission: %s\n' "${rule}"
done
