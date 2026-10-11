# Migración a Política BLACKLIST

El agente MCP ahora soporta un esquema de seguridad `blacklist` diseñado para reducir el mantenimiento por destino. Cuando este modo se activa, se asume un enfoque de "permitir todo excepto lo excluido" para entornos explícitamente administrados (Docker y File System).

Este modo se configura a través del archivo `config.yaml` y es **opt-in**. Si no se define el modo `blacklist`, el sistema funciona en modo `legacy` (por defecto), donde cada recurso individual debe figurar en una lista de permitidos.

## Diseño y Restricciones

- **Docker**:
  - `read_excludes`: Lista de contenedores ocultos a consultas (`docker_ps`, `container_inspect`, `docker_compose_logs`).
  - `mutate_excludes`: Lista de contenedores protegidos contra modificaciones (`docker_restart`).
  - *Hardcoded*: La escritura o mutación sobre los contenedores `ia-mcp-vps` y `n8n-n8n-1` está **siempre denegada**, para evitar la interrupción del propio agente de seguridad.
  - Las identidades incluyen el ID real del contenedor, los nombres, las etiquetas de *Compose Project* y *Compose Service*. Tratar de burlar las exclusiones mediante el renombramiento del contenedor o el uso de un alias no eludirá la política.

- **Files / Composiciones**:
  - `managed_roots`: Lista de rutas absolutas que el sistema puede leer de manera dinámica sin necesidad de listar individualmente cada servicio o repositorio contenido.
  - Se prohíbe explícitamente y de manera predeterminada el acceso a directorios o archivos sensibles mediante la lógica de exclusión. Ejemplos incluyen: `.ssh`, `.local`, `.config`, `.env*`, `*.key`, `*.pem`, `approvals.db` y `.db`.
  - La verificación impide el escape mediante saltos transversales de directorio (path traversal) y enlaces simbólicos.

- **Validación Estricta**:
  - Si la configuración del modo blacklist es inválida o no define listas, falla adoptando un estado cerrado, bloqueando la acción para preservar la seguridad.
  - La habilitación (`enabled: true`) y la aprobación (`requires_approval: true`) para las herramientas que mutan (como `docker_restart`) siguen siendo obligatorias; el esquema de blacklist **NO** elude las aprobaciones en Telegram, simplemente elimina la necesidad de enumerar explícitamente cada servicio bajo un entorno gestionado.

## Snippet de Migración Mínimo (Ejemplo)

Para habilitar este modo, el archivo `config.yaml` deberá actualizar su bloque de seguridad (sin eliminar la clave de validación del servicio o herramientas específicas):

```yaml
security:
  mode: "blacklist"
  docker_blacklist:
    read_excludes: ["database_interna", "auth_service"]
    mutate_excludes: ["app_critical_prod"]
  files_blacklist:
    managed_roots: ["/mnt/stacks"]
    file_excludes: ["config.production.json"]

allowed_containers: [] # Compatible pero ignorado como filtro si estás en modo blacklist

tools:
  docker_restart:
    enabled: true
    requires_approval: true
    targets: [] # En modo blacklist la política recae sobre mutate_excludes
```

*Nota*: No es necesario instalar o recuperar scripts externos; el despliegue requiere un mero reinicio del contenedor del agente tras actualizar el `config.yaml` principal.
