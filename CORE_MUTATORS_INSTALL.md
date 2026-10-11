# Core mutation gateway — installation handoff

## Delivery, not a production verification

The same Telegram workflow `kDEEBeEh7Th79HQO` is published at version `2802f762-8272-43c1-bf06-5ff4680c8a0c`. Its previous version is `0514f2af-b383-4b61-867b-51cc3d2004f0`. The native wait/approval, sole approver, callback credentials and journal remain unchanged.

This release adds/fixes `patch_file_atomic`, `git_update_ff_only`, `docker_compose_up_service`, and read-only `deployment_preflight`. Existing `docker_restart` uses the same approval gateway. Docker mutation is generic within configured projects, excluding blacklist entries and MCP/n8n control-plane targets. File writes still require explicitly writable scopes and their extension policy; secret/database/control-plane paths are excluded. Git updates require an existing credential-free HTTPS GitHub remote and a full target SHA; private and SSH remotes are unsupported.

## Manual installation

Run in the VPS terminal, not through a free-shell agent tool. Use the full SHA of the correction commit in PR33, not Jules' unpublished local SHA. Check tracked changes first; do not delete the existing untracked configuration backups.

```bash
cd /home/ubuntu/IA-mcp-vps
git diff --quiet && git diff --cached --quiet || exit 1
git fetch origin feature/core-mutations-gateway-1389829261105019987
git switch --detach EXACT_CORRECTION_SHA
bash scripts/install-core-mutators.sh EXACT_CORRECTION_SHA
```

The installer recovers the active Compose files from container labels to preserve credential overrides; backs up config and tags the currently running image; adjusts only security/tool fields; adds a read-only host-path mirror; builds and checks the Docker CLI/Compose plugin before stopping the old MCP; recreates only MCP. It prints no tokens or expanded Compose environment. Existing approval credentials must already be configured in the active Compose files. Missing files or an unsuccessful build stop the installer; installation is not guaranteed by local unit tests.

If permission is denied creating the backup directory, stop and fix that specific directory's ownership; do not run broad recursive chmod/chown or expose credentials.

## Recovery, only when deliberately requested

Use the backup path printed by the installer:

```bash
bash scripts/rollback-core-mutators.sh /home/ubuntu/.local/share/ia-mcp-vps/core-mutators-deploy/backup-TIMESTAMP
```

This restores the old config/image using the original Compose file list, without rebuilding or affecting other services. Repository checkout remains at the correction SHA. If installation stopped before recreation, restoring the config backup is sufficient to undo configuration preparation. Recovery is not automatically triggered.

## Verification after installation

Refresh the MCP connection. Confirm the new tools are exposed and `approval_enabled: True`. For the first real action, request preflight, approve its exact scope in Telegram, then query the same request ID and inspect/probe the service. A Compose success means the command returned successfully, not that service health was verified. No production mutation was executed while preparing this release.

## Test evidence / known limits

Local Python tests use FastMCP 4.1.0, not proof of the VPS runtime. The focused writer/Compose/Git/patch set passes 19 tests. Gateway and blacklist tests are run separately because their global registries interfere when combined. n8n logic: 50 existing assertions plus new action/terminal-state cases pass, without external calls. The final complete suite has 200 passing tests, 23 passing subtests and 11 failures. The exact same 11 failure names occur on the unchanged PR33 baseline; these inherited orchestrator/lazy-init failures were not included in this focused release. No Docker daemon is available in the review sandbox, so the installer performs image/CLI checks on the VPS before recreation.

Known previously accepted PR32 policy risks and the separate security-organization task are not reopened or claimed solved by this handoff.
