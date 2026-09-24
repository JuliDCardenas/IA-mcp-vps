# Uso por IA

## Conexión MCP en Notion

Nombre visible:

```txt
Servidor MCP de mi VPS
```

Nombre interno observado:

```txt
connections.mcpServer_servidor_mcp_de_mi_vps
```

No listar herramientas en cada sesión normal. Volver a listar únicamente tras cambios de esquema, despliegues o errores de herramienta inexistente.

## Caché de herramientas

Notion puede mantener un catálogo anterior. Después de agregar o renombrar herramientas:

1. reconstruir/recrear el servidor MCP;
2. consultar `listTools`;
3. si el catálogo continúa desactualizado, refrescar o recrear la conexión;
4. no llamar herramientas que no aparezcan en el catálogo.

## Herramientas operativas

### Sistema

- `system_status`
- `check_ports`
- `http_probe`

### Archivos y logs

- `list_files`
- `file_info`
- `read_file`
- `read_file_range`
- `tail_file`
- `search_text`
- `validate_yaml`
- `validate_json`

### Docker y Compose

- `docker_ps`
- `container_inspect`
- `docker_logs`
- `docker_logs_filtered`
- `docker_restart`
- `docker_compose_config`
- `docker_compose_ps`
- `docker_compose_logs`

### Git

- `git_status`

### Trabajos Agy

- `coding_job_create`
- `coding_job_status`
- `coding_job_wait`
- `coding_job_result`

Para auditoría usar `task_type=audit`. Para generar cambios aislados usar `task_type=implement`.

No confundir `NOTION_REVIEW` con un cambio publicado. Antes de promoción se deben revisar SHA base, manifiesto, hashes, artefactos, riesgos y criterios de aceptación.

## Promoción de una implementación

1. Leer `coding_job_result`.
2. Confirmar `status=NOTION_REVIEW` y `task_type=implement`.
3. Verificar que `base_commit` siga siendo válido.
4. Revisar todos los artefactos.
5. Ejecutar secret scanning con GitHub MCP.
6. Crear rama y escribir archivos mediante GitHub MCP.
7. Crear PR y revisar checks/diff.
8. Solicitar aprobación explícita.
9. Fusionar con `expectedHeadSha`.
10. Desplegar y verificar salud/logs.

Nunca escribir directamente en `main`, copiar archivos manualmente a producción ni entregar credenciales GitHub a Agy.

## Defaults GPS Tracker Logan

Log principal: `tracker-noche-2026-08-20.log`, scope `home_stacks`.

Uso preferido:

1. `tail_file` con 40-100 líneas;
2. `file_info` + `read_file_range` para rangos;
3. `search_text` para eventos puntuales.

## Reglas de eficiencia

- No leer logs completos grandes.
- Limitar archivos y criterios de trabajos Agy.
- No repetir trabajos solo para polling.
- Usar `coding_job_wait` con menos de 60 segundos cuando la conexión tenga timeout corto.
- Consultar luego con `coding_job_status` si el trabajo continúa.
- Separar observación, hipótesis y diagnóstico.
- No declarar causa raíz o despliegue exitoso con una sola señal.
