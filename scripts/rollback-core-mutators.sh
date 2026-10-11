#!/usr/bin/env bash
set -euo pipefail
umask 077
backup=${1:?Usage: rollback-core-mutators.sh BACKUP_DIRECTORY}
store=/home/ubuntu/.local/share/ia-mcp-vps/core-mutators-deploy
backup=$(realpath "$backup")
[[ "$backup" == "$store"/backup-* && -f "$backup/config.yaml" ]] || { echo "Invalid backup directory"; exit 1; }
stamp=${backup##*/backup-}
[[ "$stamp" =~ ^[0-9]{8}-[0-9]{6}$ ]] || exit 1
image="ia-mcp-vps:rollback-$stamp"
docker image inspect "$image" >/dev/null
project=$(cat "$backup/project")
files=$(cat "$backup/compose-files")
IFS=',' read -r -a paths <<< "$files"
args=(-p "$project")
for f in "${paths[@]}"; do [[ -f "$f" ]] || { echo "Missing original Compose file"; exit 1; }; args+=(-f "$f"); done
cd /home/ubuntu/IA-mcp-vps
cp -p "$backup/config.yaml" config.yaml.rollback-tmp
mv config.yaml.rollback-tmp config.yaml
if [[ -f "$backup/compose.paths.yaml" ]]; then cp -p "$backup/compose.paths.yaml" "$store/compose.paths.yaml"; fi
printf 'services:\n  ia-mcp-vps:\n    image: %s\n    pull_policy: never\n' "$image" > "$backup/compose.rollback.yaml"
args+=(-f "$backup/compose.rollback.yaml")
docker compose "${args[@]}" config --quiet
docker compose "${args[@]}" up -d --no-deps --no-build --force-recreate ia-mcp-vps
docker ps --filter name='^/ia-mcp-vps$' --format '{{.Names}} {{.Status}}'
echo 'Original image/configuration restored. Repository checkout unchanged.'
