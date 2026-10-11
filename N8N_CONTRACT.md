# n8n approval contract — core mutators

One workflow: `kDEEBeEh7Th79HQO`. Preserve authentication credentials, sole Telegram approver, callback, journal and idempotency. No additional bot consumer.

## Actual ingress envelope

Headers: `X-Event-Type`, `X-Event-Id`, `X-Approval-Webhook-Key`.
`APPROVAL_REQUESTED` JSON body has **top-level** `request_id`, `action`, `parameters`, `parameters_digest`, `expires_at` (ISO timezone), `capability_token` (in-memory worker only). There is no `payload` wrapper. Never display, log or copy the capability token to a public journal.

Actions: `approval_demo`, `docker_restart`, `patch_file_atomic`, `git_update_ff_only`, `docker_compose_up_service`.

Action parameters:
- `patch_file_atomic`: `scope`, `path`, `expected_hash`, `diff` object with exact `old`/`new`, `policy_digest`, `validate_yaml`, `validate_json`, `restore_on_failure`. Telegram shows scope/path/hash, validation options and sizes only. Never render old/new file contents; maintain payload integrity for authorization.
- `git_update_ff_only`: `scope`, `path`, `expected_base_sha`, `target_sha`, `policy_digest`, `remote`, `branch`. Telegram shows repository scope/path, branch, remote and exact SHAs. Code update is not deployment.
- `docker_compose_up_service`: `project`, `service`, `compose_path`, `project_directory` (trusted host directory), `snapshot` = `{container:{Id,Image},compose_hash}`, `policy_digest`, `allow_build`. Telegram shows exact project/service/Compose hash, existing image/id and build option. Only that service; no dependencies.

Example body (synthetic, not valid authorization):
```json
{"request_id":"a_01234567","action":"git_update_ff_only","parameters":{"scope":"sample","path":".","expected_base_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","target_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","policy_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","remote":"origin","branch":"main"},"parameters_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","expires_at":"2030-01-01T00:00:00+00:00","capability_token":"syntheticnotsecret"}
```

`APPROVAL_DECIDED`: top-level `request_id`, `decision` = APPROVED/REJECTED.
`OPERATION_COMPLETED`: top-level `request_id`, `action`, `status` (OPERATION_COMPLETED/OPERATION_FAILED/INDETERMINATE_STATE), `diagnostic` object. Reject unknown actions/states. Approval accepted is NOT operation completed. For Compose, successful exit is NOT a health check; `verification:health_check_pending` must be explicit and followed by inspect/probe from agent.
`SIMULATION_COMPLETED` retains legacy synthetic status semantics.

## Callback (unchanged)

HTTPS fixed MCP `/webhook/approval-decision`, existing Bearer credential. JSON `request_id`, `capability_token`, `decision`, `user_id`, `chat_id`. Identity must come from verified Telegram responder, not request body. Preserve expiry and deduplication. Native approve/reject wait mechanism unchanged.

## Limits

Only compact allowlisted diagnostic fields appear in Telegram; never arbitrary stdout/stderr/file contents. A local code test is not installation or production verification. The existing known PR32 policy risks are unchanged by this focused correction.

Compose path mapping: trusted tool config `host_path_map: {/mnt/stacks: /home/ubuntu}`; mirror `/home/ubuntu:/home/ubuntu:ro` in MCP worker. Approval captures mapped project directory, rechecked at execution; never accept mappings from callers.

New writer limits: secrets, databases, .git internals and control-plane paths IA-mcp-vps/n8n are not editable via core mutation tools. Explicit writable scopes required; inherited readers unchanged. Git mutator accepts existing credential-free HTTPS GitHub remotes and full SHAs, disables hooks during fetch/merge. Private/SSH remotes remain unsupported by this release.
