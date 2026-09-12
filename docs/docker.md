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
EOF
```

## Probar acceso a archivo del home

```bash
docker exec -it ia-mcp-vps ls -l /mnt/stacks/tracker-noche-2026-08-20.log
```

## Probar puerto local

```bash
curl -i http://127.0.0.1:8787
```

Puede devolver 404/405 dependiendo del path, pero debe haber respuesta HTTP del servidor.

## Notas de seguridad

- Se monta `/home/ubuntu`, no `/`.
- Se monta Docker socket, que es sensible.
- No debe existir herramienta shell libre.
- El puerto se publica solo en `127.0.0.1`; Caddy expone HTTPS con Bearer token.
