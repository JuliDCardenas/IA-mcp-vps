# Contratos de herramientas

## Reglas comunes

- Entradas y salidas acotadas.
- Errores claros y sanitizados.
- Sin secretos por defecto.
- Sin shell libre.
- Identificadores y aliases validados por el servidor.
- Acciones mutables separadas de observación y revisión.

## Sistema y archivos

- `system_status`
- `check_ports`
- `http_probe`
- `list_files`
- `file_info`
- `read_file`
- `read_file_range`
- `tail_file`
- `search_text`
- `validate_yaml`
- `validate_json`

`read_file` conserva un límite de tamaño. Para logs grandes se deben usar `file_info`, `read_file_range`, `tail_file` y `search_text`.

## Docker

- `docker_ps`
- `container_inspect`
- `docker_logs`
- `docker_logs_filtered`
- `docker_restart`

Estas herramientas usan `/var/run/docker.sock` desde el servidor MCP. Los nombres deben pertenecer a `allowed_containers`. Agy no recibe el socket.

## Compose

- `docker_compose_config`
- `docker_compose_ps`
- `docker_compose_logs`

Los proyectos, servicios y contenedores deben estar definidos en `allowed_compose_projects`.

## Git

- `git_status`

Es solo lectura y opera sobre scopes allowlisted.

## Trabajos de código

- `coding_job_create`
- `coding_job_status`
- `coding_job_wait`
- `coding_job_result`

`coding_job_create` acepta `audit` e `implement`. Un trabajo `implement` escribe únicamente en su clon temporal dentro del volumen `coding_jobs`.

`coding_job_result` es el límite de revisión y promoción. Para implementaciones contiene SHA base, manifiesto, hashes, validaciones y artefactos de texto completos. No crea ramas, commits, PR, merges ni despliegues.

La promoción se realiza exclusivamente mediante el GitHub MCP configurado para Notion IA y requiere revisión independiente.

## HTTP

`http_probe` solo acepta targets definidos en `allowed_http_targets` para evitar SSRF.
