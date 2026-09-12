# Contratos de herramientas v1

- Datos acotados.
- Errores claros.
- Sin secretos por defecto.
- Sin shell libre.

## Herramientas iniciales

- `system_status`
- `check_ports`
- `list_files`
- `file_info`
- `read_file`
- `read_file_range`
- `tail_file`
- `search_text`
- `validate_yaml`
- `validate_json`
- `docker_ps`
- `docker_logs`
- `docker_restart`

## Notas de archivos grandes

`read_file` mantiene límite de tamaño.

`read_file_range` y `tail_file` leen por streaming y sirven para logs grandes sin cargar el archivo completo al contexto.

`file_info` permite conocer `line_count` y construir un rango final sin adivinar.

`list_files` oculta rutas/nombres sensibles definidos en denylist.
