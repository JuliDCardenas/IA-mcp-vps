#!/usr/bin/env bash
set -euo pipefail
umask 077
sha=${1:?Usage: install-core-mutators.sh EXACT_SHA}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "Expected full SHA"; exit 1; }
cd /home/ubuntu/IA-mcp-vps
[[ "$(git rev-parse HEAD)" == "$sha" ]] || { echo "Repository must already be at requested SHA"; exit 1; }
# Never output Config.Env or expanded compose config.
project=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' ia-mcp-vps)
files=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' ia-mcp-vps)
[[ -n "$project" && -n "$files" ]] || { echo "Cannot recover active Compose files"; exit 1; }
IFS=',' read -r -a paths <<< "$files"
args=(-p "$project")
for f in "${paths[@]}"; do [[ -f "$f" ]] || { echo "Missing active Compose file"; exit 1; }; args+=(-f "$f"); done
store=/home/ubuntu/.local/share/ia-mcp-vps/core-mutators-deploy
mkdir -p "$store"
stamp=$(date +%Y%m%d-%H%M%S)
backup="$store/backup-$stamp"
mkdir "$backup"
cp -p config.yaml "$backup/config.yaml"
if [[ -f "$store/compose.paths.yaml" ]]; then cp -p "$store/compose.paths.yaml" "$backup/compose.paths.yaml"; fi
docker image tag "$(docker inspect --format '{{.Image}}' ia-mcp-vps)" "ia-mcp-vps:rollback-$stamp"
printf '%s\n' "$project" > "$backup/project"
printf '%s\n' "$files" > "$backup/compose-files"
# Container already includes Python/PyYAML; keep current secrets untouched.
docker exec -i ia-mcp-vps python - <<'CONFIG'
from pathlib import Path
import yaml,os,tempfile
p=Path('/mnt/stacks/IA-mcp-vps/config.yaml');stat=p.stat();cfg=yaml.safe_load(p.read_text())
s=cfg.setdefault('security',{});s['mode']='blacklist'
b=s.setdefault('docker_blacklist',{});b.setdefault('read_excludes',[])
b['mutate_excludes']=sorted(set(b.get('mutate_excludes',[])+['ia-mcp-vps','n8n-n8n-1']))
f=s.setdefault('files_blacklist',{});f.setdefault('managed_roots',['/mnt/stacks','/mnt/host-logs'])
f['file_excludes']=sorted(set(f.get('file_excludes',[])+['.env','.env.*','id_rsa','id_ed25519','authorized_keys','*.pem','*.key']))
t=cfg.setdefault('tools',{})
for name in ['docker_restart','patch_file_atomic','git_update_ff_only','docker_compose_up_service']:
    t.setdefault(name,{})['enabled']=True;t[name]['requires_approval']=True
t['docker_restart'].pop('targets',None)
t['docker_compose_up_service']['host_path_map']={'/mnt/stacks':'/home/ubuntu'}
text=yaml.safe_dump(cfg,sort_keys=False,allow_unicode=True);yaml.safe_load(text)
fd,name=tempfile.mkstemp(dir=p.parent,prefix='.core-config-')
try:
    with os.fdopen(fd,'w') as out:out.write(text);out.flush();os.fsync(out.fileno())
    os.chmod(name,stat.st_mode&0o777);os.chown(name,stat.st_uid,stat.st_gid);os.replace(name,p)
finally:
    if os.path.exists(name):os.unlink(name)
print('Configuration updated; credentials not read or printed')
CONFIG
cat > "$store/compose.paths.yaml" <<'MIRROR'
services:
  ia-mcp-vps:
    volumes:
      - /home/ubuntu:/home/ubuntu:ro
MIRROR
# Capture exact invocation for deliberate rollback (never automatically run).
printf '%q ' docker compose "${args[@]}" > "$backup/rollback-command-prefix"
printf '\n' >> "$backup/rollback-command-prefix"
args+=(-f "$store/compose.paths.yaml")
docker compose "${args[@]}" config --quiet
# Build before stopping old MCP. If build fails, restore config manually from backup; old process still running.
docker compose "${args[@]}" build ia-mcp-vps
# Prove Python and CLI work as configured user before recreating the MCP.
docker compose "${args[@]}" run --rm --no-deps --entrypoint sh ia-mcp-vps -c 'docker --version && docker compose version && python -X pycache_prefix=/tmp/mcp-install-pycache -m compileall -q /app/src'
docker compose "${args[@]}" up -d --no-deps --force-recreate ia-mcp-vps
sleep 3
docker exec ia-mcp-vps python -c "from dari_mcp_vps.config import load_config; c=load_config(); print('approval_enabled:', c.approval_enabled); print('security_mode:', c.security_mode); assert c.approval_enabled and c.security_mode == 'blacklist', 'Approval configuration not active'"
docker ps --filter name='^/ia-mcp-vps$' --format '{{.Names}} {{.Status}}'
printf 'Installed code: %s\nBackup: %s\nRollback image: ia-mcp-vps:rollback-%s\n' "$sha" "$backup" "$stamp"
