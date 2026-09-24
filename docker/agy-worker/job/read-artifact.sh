#!/usr/bin/env bash
set -euo pipefail

readonly JOB_ID="$1"
readonly PATH_B64="$2"
readonly JOB_ROOT="/var/lib/coding-jobs/${JOB_ID}"
readonly WORKTREE="${JOB_ROOT}/worktree/repo"
readonly MANIFEST="${JOB_ROOT}/manifest.json"

[[ "${JOB_ID}" =~ ^job_[0-9a-f]{32}$ ]] || exit 2
path="$(printf '%s' "${PATH_B64}" | base64 -d)"
[[ -n "${path}" && "${path}" != /* && "${path}" != *'..'* && "${path}" != .git/* ]] || exit 2

jq -e --arg path "${path}" '.changes[] | select(.path == $path and .operation == "upsert")' "${MANIFEST}" >/dev/null
resolved="$(realpath -e "${WORKTREE}/${path}")"
[[ "${resolved}" == "${WORKTREE}/"* ]] || exit 2
[[ -f "${resolved}" && ! -L "${WORKTREE}/${path}" ]] || exit 2
size="$(wc -c < "${resolved}")"
(( size <= 65536 )) || exit 3
cat "${resolved}"
