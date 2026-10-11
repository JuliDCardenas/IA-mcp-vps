# MCP Tools N8N Adaptation Contract

This document provides the exact contract schema and payload specifications for adapting the n8n approval workflow (`kDEEBeEh7Th79HQO`) to support the newly added generic mutators.

## 1. Allowed Actions (allowlist update)
The n8n workflow's validation node MUST be updated to accept the following actions, in addition to the existing `approval_demo` and `docker_restart`:
- `patch_file_atomic`
- `git_update_ff_only`
- `docker_compose_up_service`

## 2. Event Payload Schema

For all new actions, the payload sent to n8n via webhook remains structurally identical to `docker_restart`. It includes the standard wrapper and the specific snapshot/parameters embedded within `payload.diagnostic` or `payload.parameters` depending on how the initial request encodes them.
A sample schema of what n8n will receive to render the Telegram message:

### Action: patch_file_atomic
```json
{
  "request_id": "req-12345",
  "action": "patch_file_atomic",
  "status": "PENDING",
  "payload": {
    "scope": "homepage",
    "path": "settings.yaml",
    "expected_hash": "a1b2c3d4...",
    "diff": {
      "old": "enabled: false",
      "new": "enabled: true"
    },
    "policy_digest": "xyz789..."
  }
}
```
**Human Text Suggestion for Telegram:**
`File Patch Request | Scope: {{ $json.payload.scope }} | Path: {{ $json.payload.path }} | Old: {{ $json.payload.diff.old }} -> New: {{ $json.payload.diff.new }}`

### Action: git_update_ff_only
```json
{
  "request_id": "req-12345",
  "action": "git_update_ff_only",
  "status": "PENDING",
  "payload": {
    "scope": "homepage",
    "path": ".",
    "expected_base_sha": "abc1234...",
    "target_sha": "def5678...",
    "branch": "main",
    "remote": "origin",
    "policy_digest": "xyz789..."
  }
}
```
**Human Text Suggestion for Telegram:**
`Git Update Request | Scope: {{ $json.payload.scope }} | Branch: {{ $json.payload.branch }} | FF to SHA: {{ $json.payload.target_sha }}`

### Action: docker_compose_up_service
```json
{
  "request_id": "req-12345",
  "action": "docker_compose_up_service",
  "status": "PENDING",
  "payload": {
    "project": "homepage",
    "service": "web",
    "compose_path": "/mnt/stacks/homepage/docker-compose.yml",
    "allow_build": false,
    "snapshot": { ... },
    "policy_digest": "xyz789..."
  }
}
```
**Human Text Suggestion for Telegram:**
`Compose Deploy Request | Project: {{ $json.payload.project }} | Service: {{ $json.payload.service }} | Allow Build: {{ $json.payload.allow_build }}`

## 3. Webhook Callback
The `callback_data` format and token callback logic back to the MCP server's `/webhook/approval-decision` endpoint remain completely unchanged. The token is generated ephemerally by MCP and passed along without needing n8n to generate signatures.
