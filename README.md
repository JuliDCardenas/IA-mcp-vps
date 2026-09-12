# IA MCP VPS

MCP Server para mantenimiento controlado del VPS asociado a GPS Tracker Logan y al stack personal de Julián.

## Objetivo

Permitir diagnóstico y acciones acotadas sobre el VPS usando herramientas seguras, no una terminal root libre.

## Alcance v1

- Diagnóstico read-only.
- Estado básico de Docker.
- Lectura/búsqueda de archivos en rutas permitidas.
- Lectura eficiente de logs grandes por rango, cola o metadatos.
- Validación YAML/JSON.
- Base para auditoría, backups y edición por parches.

## Principios

1. Sin shell libre por defecto.
2. Sin root directo.
3. Allowlist de rutas y servicios/contenedores.
4. Denylist de secretos y rutas sensibles.
5. Cambios por patch + backup + validación.
6. Auditoría de acciones.

## Transporte remoto

El servidor usa FastMCP 2.x y corre por HTTP en el puerto interno `8787`.

Docker publica solo en localhost del host:

```txt
127.0.0.1:8787:8787
```

Caddy expone HTTPS en:

```txt
https://mcp.julidcardenas.site/mcp
```

con Bearer token en el reverse proxy. Ver [`docs/caddy.md`](docs/caddy.md).

## Uso por IA

Ver [`docs/ai-usage.md`](docs/ai-usage.md) para defaults operativos, herramientas disponibles y log principal del proyecto GPS.

## Ejecución con Docker

```bash
cd ~/IA-mcp-vps
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
docker logs --tail 100 ia-mcp-vps
```

Más detalle en [`docs/docker.md`](docs/docker.md).

## Estado

Bootstrap remoto inicial. No agregar herramientas destructivas sin guardrails y confirmación.
