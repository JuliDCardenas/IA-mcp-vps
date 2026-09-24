#!/usr/bin/env bash
set -euo pipefail

readonly SETTINGS_DIR="${HOME}/.gemini/antigravity-cli"
readonly SETTINGS_FILE="${SETTINGS_DIR}/settings.json"
readonly TMP_FILE="$(mktemp)"
readonly RULE='read_file(/workspace/IA-mcp-vps)'

mkdir -p "${SETTINGS_DIR}"

if [[ -s "${SETTINGS_FILE}" ]]; then
  jq --arg rule "${RULE}" \
    '.permissions = (.permissions // {}) | .permissions.allow = (((.permissions.allow // []) + [$rule]) | unique)' \
    "${SETTINGS_FILE}" > "${TMP_FILE}"
else
  jq -n --arg rule "${RULE}" '{permissions: {allow: [$rule]}}' > "${TMP_FILE}"
fi

install -m 0600 "${TMP_FILE}" "${SETTINGS_FILE}"
rm -f "${TMP_FILE}"

jq -e --arg rule "${RULE}" '.permissions.allow | index($rule) != null' "${SETTINGS_FILE}" >/dev/null
printf 'Configured scoped permission: %s\n' "${RULE}"
