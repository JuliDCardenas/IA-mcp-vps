# IA MCP VPS

MCP Server para mantenimiento controlado del VPS asociado a GPS Tracker Logan.

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

## Ejecución con Docker

```bash
git pull
cp config.docker.example.yaml config.yaml
# editar config.yaml si las rutas/contenedores reales cambian
docker compose up -d --build
docker logs -f ia-mcp-vps
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
