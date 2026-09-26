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
   - Transiciona a `CHANGES_APPROVED`. No publica.

9. **`coding_job_publish_branch`**:
   - Publica la rama de característica aprobada a través de `BranchPromoter`.
   - Solo permitido desde `CHANGES_APPROVED`.
   - Prohíbe publicación a `main` y prohíbe push forzado. Falla cerrado sin configuración.

10. **`coding_job_create_pull_request`**:
    - Crea el Pull Request en GitHub para la rama publicada.
    - Solo permitido desde `BRANCH_PUBLISHED`. Falla cerrado sin token.

11. **`coding_job_cancel`**:
    - Cancela de forma idempotente un trabajo activo. Preserva el worktree.

12. **`coding_job_cleanup`**:
    - Limpieza idempotente y segura del worktree asignado.
    - Rechaza worktrees sucios, cambios no publicados y trabajos activos. Preserva el repositorio bare base.
