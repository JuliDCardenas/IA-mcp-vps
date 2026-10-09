# Contratos de herramientas

## Reglas comunes

- Entradas y salidas acotadas y estructuradas.
- Errores claros, tipados y sanitizados.
- Sin secretos ni credenciales en respuestas o logs.
- Sin shell libre ni ejecución de comandos arbitrarios provistos por el usuario.
- Argumentos subprocess como listas fijas (`shell=False`).
- Identificadores, rutas y aliases validados estrictamente por el servidor.
- Acciones mutables separadas rigurosamente de observación, validación y revisión.
- Comportamiento fail-closed ante credenciales o configuración ausente.

---

## Sistema y archivos

- `system_status`: Estado global del sistema y métricas.
- `check_ports`: Puertos abiertos locales.
- `http_probe`: Verificación HTTP sobre destinos allowlisted (`allowed_http_targets`) para prevenir SSRF.
- `list_files`, `file_info`, `read_file`, `read_file_range`, `tail_file`, `search_text`: Operaciones de archivo confinadas bajo el root configurado con límites de tamaño estrictos.
- `validate_yaml`, `validate_json`: Validación de esquemas y sintaxis estructurada.

---

## Docker y Compose

- `docker_ps`, `container_inspect`, `docker_logs`, `docker_logs_filtered`, `docker_restart`: Control de contenedores allowlisted (`allowed_containers`) a través de socket local.
- `docker_compose_config`, `docker_compose_ps`, `docker_compose_logs`: Proyectos y servicios restringidos a `allowed_compose_projects`.
- **Agy no recibe acceso al socket de Docker.**

---

## Git

- `git_status`: Inspección de estado de trabajo en solo lectura sobre rutas allowlisted.

---

## Orquestador de trabajos de código (`coding_job_*`)

El subsistema expone 12 herramientas integradas para trabajos de auditoría e implementación (tanto en modo heredado como en modo persistente con worktrees):

1. **`coding_job_create`**:
   - Inicia un trabajo asíncrono.
   - Parámetros: `repository` (alias allowlisted), `task_type` (`audit` | `implement`), `goal`, `acceptance_criteria`, `constraints`, `base_branch` (default `main`), `work_item_id` (opcional), `execution_mode` (`legacy` | `persistent`).
   - Devuelve: `job_id`, `status` (`CREATED`), `work_item_id`, `feature_branch`.

2. **`coding_job_status`**:
   - Devuelve estado, fase, timestamps, revisiones y errores sanitizados.

3. **`coding_job_wait`**:
   - Sondeo acotado (hasta 120s) hasta alcanzar un estado terminal o `NOTION_REVIEW`.

4. **`coding_job_result`**:
   - Devuelve el informe de validación determinista, manifiesto, o diagnóstico sanitizado.

5. **`coding_job_changes`**:
   - Devuelve el manifiesto de cambios validados (rutas, operaciones `upsert`/`delete`, tamaños y hashes SHA-256).

6. **`coding_job_artifact`**:
   - Devuelve el contenido de un archivo validado dentro del worktree de la tarea.

7. **`coding_job_request_revision`**:
   - Solicita una revisión en modo persistente reutilizando `job_id`, `conversation_id`, rama y worktree.
   - Máximo 3 revisiones por trabajo. Solo permitido desde `NOTION_REVIEW`.

8. **`coding_job_approve_changes`**:
   - Aprueba los cambios validados desde `NOTION_REVIEW`.
   - Requiere `expected_validation_hash` y `expected_base_commit` coincidentes.
   - Crea exactamente un commit local en la rama de característica con identidad determinista del orquestador y persiste `approved_commit_sha`.
   - Transiciona a `CHANGES_APPROVED`. No publica.

9. **`coding_job_publish_branch`**:
   - Publica la rama de característica aprobada a través de `BranchPromoter`.
   - Requiere `approved_commit_sha` y verifica coincidencia con el ref de la rama.
   - Solo permitido desde `CHANGES_APPROVED`.
   - Prohíbe publicación a `main` y prohíbe push forzado. Falla cerrado sin configuración.

10. **`coding_job_create_pull_request`**:
    - Crea el Pull Request en GitHub para la rama publicada.
    - Solo permitido desde `BRANCH_PUBLISHED`. Falla cerrado sin token.

11. **`coding_job_cancel`**:
    - Cancela de forma idempotente un trabajo activo. Preserva el worktree.

12. **`coding_job_cleanup`**:
    - Limpieza idempotente y segura del worktree asignado.
    - Parámetros: `job_id`, `confirm_discard_unpublished: bool = false`.
    - Por defecto rechaza worktrees sucios, cambios no publicados y trabajos activos.
    - Con `confirm_discard_unpublished=true`, permite descarte forzado exclusivamente para estados terminales `CANCELLED` o `FAILED`.
    - Preserva el repositorio bare base y worktrees hermanos.

## Entorno de contenedores

- `discover_containers`: Inspección limitada y de solo lectura de la API Docker para retornar candidatos. Devuelve atributos seguros sin exponer mounts, paths, environment ni outputs de error en crudo.
- `discover_compose_projects`: Localiza metadatos de configuración en rutas seguras (`docker-compose.yml`, etc). Aplica restricciones completas de paths (denied files, limites de tamaño) mediante una búsqueda iterativa justa (BFS/level-order) para encontrar directorios de servicios explícitos antes de agotar el presupuesto en subdirectorios profundos.
- `discover_http_targets`: Proyecta servicios HTTP probables. Identifica destinos de forma segura, diferenciando puertos mapeados de puertos privados inalcanzables. No ejecuta pruebas de red a nuevos puertos no publicados ni deduce alcanzabilidad real sin evidencia.
- `suggest_allowlist_updates`: Compara la lista de configuración (`allowlists`) con los componentes descubiertos y genera fragmentos en YAML de validación manual para actualización segura de dependencias. Omite sugerencias de mapeos conservadores ambiguos (ej. loopback IP no configuradas explícitamente en el orquestador). Desduplica orígenes sin descartar rutas de endpoints ya configurados. Nunca altera ni reinicia servicios por sí mismo.

### Límites Finitos

Todos los endpoints de descubrimiento aplican límites de seguridad robustos:
- Las llamadas que devuelven listas aplican un límite estricto de elementos a devolver (`limit = max(1, min(limit, 100))`). Incluyen un atributo booleano global `truncated` indicando si los resultados fueron limitados.
- Adicionalmente el escáner del `docker-compose.yml` retorna un atributo `metadata_truncated` por proyecto individual, indicando si los resultados de los `services`, `networks` o `volumes` excedieron el máximo configurado por el agregador (20).
- El descubrimiento de metadatos mediante `os.walk` implementa un presupuesto máximo de inspección estricto de 200 directorios visitados antes de detenerse y devolver resultados parciales (`truncated = True`).
- Las lecturas de los archivos (ej. metadatos en YAML) evalúan la restricción `max_file_bytes` de la configuración antes de la lectura.

### Despliegue Manual y Recuperación (Revert)

Estas herramientas solo son de propósito de **visualización y descubrimiento**. Para llevar a cabo un despliegue de las sugerencias devueltas:

1. **Aprobación manual:** Un administrador deberá leer y validar manualmente el bloque en formato YAML emitido por `suggest_allowlist_updates`.
2. **Aplicar cambios:** Copiar las reglas deseadas (contenedores permitidos, proyectos permitidos, objetivos HTTP) al archivo `config.yaml` o entorno equivalente desplegado.
3. **Revertir:** En caso de que se presente alguna regresión u operación no esperada debido a las nuevas reglas permitidas, la recuperación se logrará mediante la eliminación estricta y manual de dichas reglas desde el archivo de configuración afectado, seguido de un reinicio completo de las herramientas (`systemctl restart ia-mcp-vps` o el reinicio del contenedor).
