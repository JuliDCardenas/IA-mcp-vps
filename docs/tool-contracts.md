# Contratos de herramientas v1

- Datos acotados.
- Errores claros.
- Sin secretos por defecto.
- Sin shell libre.

## Herramientas actuales

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
- `docker_ps`
- `container_inspect`
- `docker_logs`
- `docker_logs_filtered`
- `docker_restart`
- `docker_compose_config`
- `docker_compose_ps`
- `docker_compose_logs`
- `git_status`

## Docker

Las herramientas Docker usan `/var/run/docker.sock` vía HTTP Unix socket. No dependen del binario `docker` dentro del contenedor.

`docker_restart` requiere contenedor en allowlist.

## Compose

Las herramientas Compose sí usan el binario `docker compose`; dependen del socket Docker y de que el binario esté disponible en la imagen.

## HTTP

`http_probe` solo acepta targets definidos en `allowed_http_targets` para evitar SSRF.

## Git

`git_status` es solo lectura y debe ejecutarse sobre un scope allowlisted.

## Archivos grandes

`read_file` mantiene límite de tamaño.

`read_file_range` y `tail_file` leen por streaming y sirven para logs grandes sin cargar el archivo completo al contexto.

`file_info` permite conocer `line_count` y construir un rango final sin adivinar.
