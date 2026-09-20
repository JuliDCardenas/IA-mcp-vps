# Uso por IA

## Conexión MCP en Notion

Nombre visible configurado en Notion:

```txt
Servidor MCP de mi VPS
```

Nombre interno observado desde Notion AI:

```txt
connections.mcpServer_servidor_mcp_de_mi_vps
```

No listar herramientas en cada sesión normal. Solo volver a listar si una llamada falla por herramienta inexistente/cambio de esquema o si se agregaron herramientas nuevas y se necesita refrescar.

## Regla de reconexión en Notion

Cuando se agreguen, eliminen o renombren herramientas, Notion puede mantener cacheada la lista anterior. Julián debe eliminar/recrear o refrescar la conexión MCP, ingresando de nuevo nombre, URL y Bearer token.

## Herramientas disponibles v1

### Sistema

- `system_status`: salud general del VPS.
- `check_ports`: prueba puertos TCP desde el VPS.

### Archivos/logs

- `list_files(scope, path='.')`: lista archivos no sensibles dentro de un scope.
- `file_info(scope, path, count_lines=true)`: devuelve tamaño, modificación y conteo de líneas.
- `read_file(scope, path)`: lee archivo UTF-8 dentro de un scope permitido, con límite de tamaño.
- `read_file_range(scope, path, start_line, end_line)`: lee solo un rango de líneas.
- `tail_file(scope, path, lines=100)`: últimas líneas de un archivo grande.
- `search_text(scope, query, path='.')`: búsqueda textual con ripgrep.

### Docker

- `docker_ps`: lista contenedores usando Docker socket, no CLI.
- `container_inspect(container)`: estado, health, mounts, puertos y restart policy de un contenedor permitido.
- `docker_logs(container, lines=100)`: logs recientes de contenedor permitido.
- `docker_logs_filtered(container, lines=200, grep=null, case_sensitive=false, since=null)`: logs filtrados.
- `docker_restart(container)`: reinicia contenedor permitido.

### Git

- `git_status(scope, path='.')`: estado git read-only de un repo allowlisted.

### Validación

- `validate_yaml(scope, path)`
- `validate_json(scope, path)`

## Defaults del proyecto GPS Tracker Logan

### Log MQTT principal de campo

Scope:

```txt
home_stacks
```

Archivo:

```txt
tracker-noche-2026-08-20.log
```

Uso preferido:

1. Para saber el final del archivo:
   - `tail_file(scope='home_stacks', path='tracker-noche-2026-08-20.log', lines=40)`
2. Si `tail_file` no aparece todavía en Notion por caché de herramientas:
   - `file_info(...)` para obtener `line_count`.
   - `read_file_range(..., start_line=line_count-40, end_line=line_count)`.
3. Para eventos puntuales:
   - `search_text(scope='home_stacks', query='parked_sleep', path='tracker-noche-2026-08-20.log')`
   - `search_text(scope='home_stacks', query='engine_off', path='tracker-noche-2026-08-20.log')`
   - `search_text(scope='home_stacks', query='sys/wake', path='tracker-noche-2026-08-20.log')`

### Repo MCP

Scope:

```txt
ia_mcp_vps
```

Uso:

- `git_status(scope='ia_mcp_vps')`
- `read_file(scope='ia_mcp_vps', path='README.md')`

## Reglas de eficiencia

- No leer logs completos grandes.
- Usar `tail_file`, `file_info`, `read_file_range` y `search_text` antes que `read_file`.
- Para diagnóstico, consultar primero evidencias pequeñas: últimas 40-100 líneas, estado de contenedores y búsquedas específicas.
- Separar observación, hipótesis y diagnóstico.
- No declarar causa raíz con una sola señal.
