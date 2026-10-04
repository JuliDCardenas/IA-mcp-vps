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

## Configuración de Jules (Milestone 1)

El orquestador incluye soporte parcial asíncrono para delegar tareas al agente Jules sin esperar su finalización. Esto requiere las siguientes variables de entorno:

- `JULES_API_KEY`: Tu clave de API para la plataforma Jules.
- `JULES_API_URL`: Opcional, por defecto `https://jules.googleapis.com/v1alpha`.
- `JULES_DB_PATH`: Opcional, por defecto `/var/lib/coding-jobs/jules_jobs.db`.
- `N8N_WEBHOOK_URL`: URL del webhook HTTP creado en n8n para recibir eventos (ej: `https://n8n.julidcardenas.site/webhook/jules-notifications`).
- `N8N_WEBHOOK_KEY`: Clave secreta (X-Jules-Webhook-Key) para autenticar las peticiones contra n8n.

**Configuración segura en el VPS:**
1. Crear o editar el archivo `.env` en la raíz del proyecto (`~/IA-mcp-vps/.env`).
2. Añadir las claves de forma segura sin comillas:
   ```env
   JULES_API_KEY=tu_clave_real_aqui
   N8N_WEBHOOK_URL=https://n8n.julidcardenas.site/webhook/jules-notifications
   N8N_WEBHOOK_KEY=tu_secreto_n8n_aqui
   ```
3. Reiniciar el contenedor: `docker compose up -d ia-mcp-vps`

Las herramientas disponibles son `jules_request_coding_task` y `jules_check_task_status`.

## Categorías de Herramientas MCP (Tags)

El servidor organiza sus herramientas en cuatro grupos principales para facilitar su descubrimiento y uso:

- **vps**: Diagnóstico, puertos, Docker, Compose, logs, comprobaciones HTTP.
  - *Herramientas:* `system_status`, `check_ports`, `docker_ps`, `container_inspect`, `docker_logs`, `docker_logs_filtered`, `docker_restart`, `docker_compose_config`, `docker_compose_ps`, `docker_compose_logs`, `http_probe`.
- **repositorios_archivos**: Operaciones con el sistema de archivos local, validación de formatos y estado Git de solo lectura.
  - *Herramientas:* `list_files`, `file_info`, `read_file`, `read_file_range`, `tail_file`, `search_text`, `validate_yaml`, `validate_json`, `git_status`.
- **agy**: Ciclo de vida completo de los trabajos de código aislados (Antigravity).
  - *Herramientas:* `coding_repository_list`, `coding_job_create`, `coding_job_status`, `coding_job_wait`, `coding_job_result`, `coding_job_changes`, `coding_job_artifact`, `coding_job_request_revision`, `coding_job_validate_only`, `coding_job_apply_mechanical_operation`, `coding_job_approve_changes`, `coding_job_publish_branch`, `coding_job_create_pull_request`, `coding_job_cancel`, `coding_job_cleanup`, `coding_private_job_create`.
- **jules**: Integración con el orquestador Jules.
  - *Herramientas:* `jules_request_coding_task`, `jules_reply_to_task`, `jules_check_task_status`.

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
