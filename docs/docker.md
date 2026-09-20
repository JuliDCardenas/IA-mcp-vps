# Ejecución con Docker

## Contexto del VPS actual

Los stacks viven bajo `/home/ubuntu` en el host. El compose monta `/home/ubuntu` como `/mnt/stacks` dentro del contenedor.

## Actualizar y reconstruir

```bash
cd ~/IA-mcp-vps
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
docker ps --filter name=ia-mcp-vps
docker logs --tail 100 ia-mcp-vps
```

## Config real

```bash
cp config.docker.example.yaml config.yaml
nano config.yaml
```

## UID/GID

El compose corre por defecto como `1001:1001`, que corresponde al usuario `ubuntu` de este VPS. Si cambia:

```bash
id
cat > .env <<'EOF'
MCP_UID=1001
MCP_GID=1001
MCP_DOCKER_GID=999
EOF
```

Ajusta `MCP_DOCKER_GID` con el grupo real del socket Docker:

```bash
stat -c '%g %G %a %n' /var/run/docker.sock
```

En este VPS se observó que el grupo `docker` es GID `999`, por eso el compose usa `group_add` con `999` por defecto. Sin ese grupo, las herramientas Docker fallan con `Permission denied`.

## Probar acceso a archivo del home

```bash
docker exec -it ia-mcp-vps ls -l /mnt/stacks/tracker-noche-2026-08-20.log
```

## Probar Docker socket desde contenedor

```bash
docker exec -it ia-mcp-vps python - <<'PY'
import socket
s=socket.socket(socket.AF_UNIX)
s.connect('/var/run/docker.sock')
print('docker socket ok')
PY
```

## Probar puerto local

```bash
curl -i http://127.0.0.1:8787
```

Puede devolver 404/405 dependiendo del path, pero debe haber respuesta HTTP del servidor.

## Notas de seguridad

- Se monta `/home/ubuntu`, no `/`.
- Se monta Docker socket, que es sensible.
- El contenedor se agrega al grupo del Docker socket, no corre como root.
- No debe existir herramienta shell libre.
- El puerto se publica solo en `127.0.0.1`; Caddy expone HTTPS con Bearer token.
