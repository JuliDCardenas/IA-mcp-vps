# IA MCP VPS

MCP Server para mantenimiento controlado del VPS asociado a GPS Tracker Logan y al stack personal de Julián.

## Objetivo

Permitir diagnóstico y acciones acotadas sobre el VPS usando herramientas seguras, no una terminal root libre.

## Alcance v1

- Diagnóstico read-only.
- Estado básico de Docker.
- Lectura/búsqueda de archivos en rutas permitidas.
- Validación YAML/JSON.
- Base para auditoría, backups y edición por parches.

## Principios

1. Sin shell libre por defecto.
2. Sin root directo.
3. Allowlist de rutas y servicios/contenedores.
4. Denylist de secretos y rutas sensibles.
5. Cambios por patch + backup + validación.
6. Auditoría de acciones.

## VPS actual

El compose asume que los stacks viven en `/home/ubuntu` y se montan dentro del contenedor como `/mnt/stacks`.

El contenedor corre por defecto como UID/GID `1000:1000`, que normalmente corresponde a `ubuntu`, para poder leer `/home/ubuntu` sin correr como root.

## Ejecución con Docker

```bash
cd ~/IA-mcp-vps
git pull
cp config.docker.example.yaml config.yaml
# revisar nombres reales de contenedores
docker ps --format '{{.Names}}'
# editar config.yaml si rutas/contenedores reales cambian
docker compose up -d --build
docker logs -f ia-mcp-vps
```

Si el contenedor queda reiniciando o venías de una imagen anterior:

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
docker ps --filter name=ia-mcp-vps
docker logs --tail 100 ia-mcp-vps
```

Más detalle en [`docs/docker.md`](docs/docker.md).

## Ejecución sin Docker

En Ubuntu normalmente usar `python3`, no `python`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
python3 -m dari_mcp_vps.server
```

## Estado

Bootstrap inicial. No exponer a Internet hasta completar transporte remoto, autenticación y pruebas.
