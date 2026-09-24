# Agy worker bootstrap

## Purpose

Run Antigravity CLI in an isolated container before implementing the asynchronous coding-job bridge.

This bootstrap does **not** expose an MCP tool, clone a repository, execute a coding job, or deploy application changes.

## Security boundary

The worker intentionally has:

- no Docker socket;
- no host bind mounts;
- no access to `/home/ubuntu`, `/mnt/stacks`, `/config`, SSH keys, or `.env` files;
- a non-root UID/GID (`10001:10001`);
- a read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges` enabled;
- bounded processes, memory, and CPU;
- dedicated named volumes for the Agy profile, workspace, and future job data.

The default Compose network still permits outbound traffic. Restrict egress in a later hardening step after the required Google and GitHub endpoints are verified.

## Supported host

The initial target is the VPS confirmed as Linux `aarch64` with Docker Compose `v5.4.0`. The official installer advertises native Linux support and detects the platform during the image build. A failed ARM64 download must stop the build; do not substitute an unofficial binary.

## Build without starting

```bash
git pull --ff-only
docker compose -f docker-compose.agy-worker.yml build --no-cache agy-worker
```

Review the build output. It must finish with a successful `agy --version` check.

## Start the isolated worker

```bash
docker compose -f docker-compose.agy-worker.yml up -d agy-worker
docker compose -f docker-compose.agy-worker.yml ps
docker compose -f docker-compose.agy-worker.yml exec agy-worker agy --version
```

## Authenticate once

Start an interactive session:

```bash
docker compose -f docker-compose.agy-worker.yml exec agy-worker agy
```

On a remote server, Agy should print an authorization URL. Open it locally, authenticate with the approved Google account, and paste the returned authorization code into the terminal. Never paste the code, cached profile, or tokens into Git, Notion, logs, or chat.

The profile is retained in the `agy_home` named volume.

## Headless smoke test

After authentication:

```bash
docker compose -f docker-compose.agy-worker.yml exec agy-worker \
  agy -p 'Return only a short confirmation that headless mode works. Do not use tools.' \
  --output-format json \
  --json-schema '{"type":"object","properties":{"ok":{"type":"boolean"},"message":{"type":"string"}},"required":["ok","message"],"additionalProperties":false}' \
  --sandbox \
  --print-timeout 2m
```

Expected properties:

- process exits successfully;
- stdout is one JSON envelope;
- `structured_output.ok` is `true`;
- no approval bypass flag is used;
- no host path becomes visible.

## Verification

```bash
docker inspect agy-worker --format '{{range .Mounts}}{{println .Type .Destination}}{{end}}'
docker inspect agy-worker --format '{{json .HostConfig.CapDrop}}'
docker inspect agy-worker --format '{{json .HostConfig.SecurityOpt}}'
docker inspect agy-worker --format '{{.HostConfig.ReadonlyRootfs}}'
```

All reported mount types must be `volume`; no entry may have type `bind`. Some Docker versions also list named-volume declarations under `HostConfig.Binds`, so that field alone is not a reliable host-bind test.

## Stop and remove the container

```bash
docker compose -f docker-compose.agy-worker.yml down
```

This preserves named volumes. Do not add `-v` unless the Agy profile, workspace, and job data are intentionally being destroyed.

## Explicitly out of scope

- mounting the real repository;
- Git credentials or SSH keys;
- `coding_job_*` MCP tools;
- worktrees and temporary branches;
- process execution from the MCP;
- `--dangerously-skip-permissions`;
- PR, merge, deployment, or Docker control from Agy.
