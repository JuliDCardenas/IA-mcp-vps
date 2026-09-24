# IA MCP VPS

Servidor MCP para mantenimiento controlado del VPS y orquestación segura de trabajos de código con Antigravity (Agy).

## Objetivo

Permitir diagnóstico, acciones operativas acotadas y trabajos de código aislados sin exponer una terminal root libre ni permitir escrituras directas a `main`.

## Capacidades

- Diagnóstico read-only del VPS.
- Estado, inspección, logs y reinicio allowlisted de Docker.
- Lectura y búsqueda de archivos en rutas permitidas.
- Validación YAML/JSON.
- Trabajos Agy asíncronos de auditoría (`task_type=audit`).
- Trabajos Agy de implementación en clones desechables (`task_type=implement`).
- Validación independiente de rutas, tamaño, secretos, diff y sintaxis.
- Promoción por rama y Pull Request mediante el GitHub MCP, fuera del contenedor Agy.

## Principios

1. Sin shell libre por defecto.
2. Sin root directo.
3. Allowlist de rutas, repositorios, servicios y contenedores.
4. Denylist de secretos y rutas sensibles.
5. Agy no recibe credenciales de GitHub ni socket Docker.
6. Ningún trabajo escribe directamente en `main`.
7. Merge y despliegue requieren revisión, SHA esperado y aprobación explícita.
8. Auditoría y resultados estructurados antes de promover cambios.

## Flujo de código

```text
Notion IA
  → coding_job_create
  → clon temporal del main actual
  → propuesta estructurada de Agy
  → aplicación y validación en el clon
  → NOTION_REVIEW
  → rama/PR mediante GitHub MCP
  → aprobación del usuario
  → merge con SHA esperado
  → despliegue controlado
```

Consulta [`docs/coding-jobs.md`](docs/coding-jobs.md) para contratos, estados, límites y procedimiento de promoción.

## Transporte remoto

FastMCP corre por HTTP en el puerto interno `8787`. Docker publica únicamente:

```txt
127.0.0.1:8787:8787
```

Caddy expone:

```txt
https://mcp.julidcardenas.site/mcp
```

con Bearer token en el reverse proxy. Ver [`docs/caddy.md`](docs/caddy.md).

## Uso por IA

Ver [`docs/ai-usage.md`](docs/ai-usage.md) para defaults operativos y herramientas disponibles.

## Despliegue

```bash
cd ~/IA-mcp-vps
git pull --ff-only
docker compose build ia-mcp-vps
docker compose up -d --force-recreate ia-mcp-vps

docker compose -f docker-compose.agy-worker.yml build agy-worker
docker compose -f docker-compose.agy-worker.yml up -d --force-recreate agy-worker
```

Más detalle en [`docs/deployment.md`](docs/deployment.md).

## Estado

La auditoría asíncrona está operativa. La implementación aislada permanece en validación hasta completar prueba de promoción rama → PR → aprobación → merge → despliegue.
