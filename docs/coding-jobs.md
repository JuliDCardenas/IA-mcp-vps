# Orquestación segura de trabajos de código

## Alcance

El subsistema `coding_job_*` permite que Notion IA y clientes MCP encarguen auditorías e implementaciones a Agy sin entregar al agente acceso directo a GitHub, Docker, producción o `main`.

El orquestador soporta dos modos de ejecución compatibles:
1. **Modo heredado (Legacy):** Ejecución aislada donde Agy propone cambios estructurados en JSON aplicados por scripts fijos sobre clones efímeros.
2. **Modo persistente (Persistent Worktrees):** Edición directa en worktrees Git persistentes administrados por `WorktreeManager`, con ramas de características dedicadas, ciclos de revisión acotados (hasta 3), validación determinista e interfaz de promoción independiente.

Los repositorios permitidos se resuelven exclusivamente mediante allowlist en `RepositoryPolicy` (`ia_mcp_vps`, `repositorio_bd_emision`). La rama base por defecto es `main`.

---

## Ciclo de vida y estados

```text
CREATED
  → PREPARING
  → RUNNING
  → VALIDATING
  → NOTION_REVIEW
      ↳ REVISION_REQUESTED → RUNNING (máximo 3 revisiones)
      ↳ CHANGES_APPROVED
          → BRANCH_PUBLISHED
          → PR_CREATED
          → CLEANED_UP
```

### Estados terminales alternativos:
- `FAILED`: Fallo de ejecución, sintaxis, secretos o validación.
- `CANCELLED`: Cancelación idempotente por el usuario; preserva el worktree para inspección forense.
- `EXPIRED`: Vencimiento del tiempo de espera acotado.
- `BLOCKED_DEPLOYMENT`: Estado fail-closed alcanzado cuando se solicita ejecución persistente pero el despliegue carece del ejecutor de contenedores efímeros aislados por trabajo.

`NOTION_REVIEW` indica únicamente que la evidencia estructurada y el hash determinista están listos para revisión independiente. **Bajo ninguna circunstancia se realizan fusiones (merges) automáticas ni despliegues a producción.**

### Arquitectura del ejecutor de contenedores efímeros aislados (DockerAgyJobRunner)

El contenedor orquestador del servidor MCP (`ia-mcp-vps`) posee acceso a `/var/run/docker.sock`, `/home/ubuntu` (`/mnt/stacks`), `./config.yaml` y a los repositorios base permanentes. Ejecutar Agy directamente dentro del contenedor MCP o reutilizar un worker multitenant expondría el host y secretos.

Por ello, el orquestador implementa `DockerAgyJobRunner`, el cual utiliza la API de Docker para aprovisionar **un contenedor efímero aislado por trabajo (`agy-job-{job_id}`)** con las siguientes garantías:
1. **Separación estricta de rutas de almacenamiento:**
   - Ruta interna del contenedor MCP (`storage_root`): `/var/lib/coding-jobs`.
   - Ruta absoluta en el host (`host_storage_root`): `/home/ubuntu/.local/share/ia-mcp-vps/coding-jobs`.
   - Prerrequisito de despliegue en el host:
     ```bash
     sudo install -d -o 10001 -g 10001 -m 0700 \
       /home/ubuntu/.local/share/ia-mcp-vps/coding-jobs
     ```
   - El servicio `ia-mcp-vps` en `docker-compose.yml` monta:
     `/home/ubuntu/.local/share/ia-mcp-vps/coding-jobs:/var/lib/coding-jobs`.
2. **Montaje exclusivo en el contenedor de Agy:**
   - Monta únicamente el worktree exacto asignado en `/workspace:rw` (traducido deterministamente desde `host_storage_root / "worktrees" / <alias> / <work_item_id>`). El contenedor de Agy jamás ve `/home/ubuntu` ni el directorio padre en su sistema de archivos.
   - Monta el volumen verificado de autenticación de Agy: `agy-worker_agy_home:/home/agy:rw`.
   - Monta un sistema de archivos temporal en memoria: `tmpfs: /tmp:rw,noexec,nosuid,size=256m`.
3. **Cero exposición de secretos o host:** El contenedor de Agy jamás recibe `/var/run/docker.sock`, `/config`, `/home/ubuntu`, `/mnt/stacks`, llaves de despliegue privadas ni tokens de publicación.
4. **Restricciones de seguridad extremas:** Ejecuta como usuario no privilegiado `10001:10001`, con sistema de archivos raíz de solo lectura (`read_only: true`), eliminación de todas las capacidades (`cap_drop: [ALL]`), `no-new-privileges: true`, límites estrictos de recursos (`pids_limit: 256`, `mem_limit: 2g`, `cpus: 2.0`) y sin puertos publicados (`PortBindings: {}`, `PublishAllPorts: false`).
5. **Red dedicada controlada (`NetworkMode: agy-worker_default`):**
   - Utiliza exclusivamente la red allowlisted del worker (`agy-worker_default`), permitiendo acceso saliente acotado hacia Google/Antigravity sin publicar ningún puerto hacia el host ni hacia internet.
   - Cualquier intento de configurar redes arbitrarias (`host`, `none`, `container:*`, `bridge`) es rechazado con `SecurityError`.
   - *Endurecimiento residual:* El filtrado de egress a nivel de dominio (DNS/proxy allowlist) queda documentado como ítem residual de infraestructura para despliegues con proxy corporativo disponible.
6. **Comando y entorno inmutables:** Ejecuta el comando fijo de Agy 1.2.11 (`agy [-p ... | --conversation ... -p ...] --mode=accept-edits --sandbox --print-timeout 20m --output-format json`) con entorno mínimo (`HOME=/home/agy`, `PATH=...`, `LANG=C.UTF-8`, `TMPDIR=/tmp`).
7. **Ciclo de vida efímero:** El contenedor se inicia, espera de forma acotada (timeout configurable, por defecto 1200s), captura y sanitiza los registros, y se detiene y elimina deterministamente en un bloque `finally`.
8. **Cancelación segura:** `coding_job_cancel` busca y detiene únicamente contenedores que contengan la etiqueta exacta `ia_mcp_vps.job_id={job_id}`.
9. **Comportamiento Fail-Closed:** Ante la ausencia de `orchestrator.host_storage_root`, red inválida o con `runner_enabled: false` (el valor por defecto), el trabajo transiciona de forma segura a `BLOCKED_DEPLOYMENT` sin ejecutar código.
10. **Repositorios privados:** El modo persistente rechaza de forma fail-closed `repositorio_bd_emision` para prevenir exposición de la llave de despliegue a contenedores de Agy, preservando intacto el flujo heredado `coding_private_job_create`.

---

## Herramientas MCP expuestas

### 1. `coding_job_create`
Crea un trabajo asíncrono y devuelve inmediatamente un `job_id` generado por el servidor (`job_<uuid32>`).
- `repository`: Alias allowlisted (ej. `ia_mcp_vps`).
- `task_type`: `audit` o `implement`.
- `goal`: Objetivo acotado (1-4000 caracteres).
- `acceptance_criteria`: Lista de criterios verificables.
- `constraints`: Restricciones operativas opcionales.
- `base_branch`: Rama base (`main`).
- `work_item_id`: (Opcional, modo persistente) Identificador seguro de la característica/tarea.
- `execution_mode`: `legacy` (por defecto retrocompatible) o `persistent`.

### 2. `coding_job_status`
Devuelve el estado actual, fase, timestamps, revisiones y errores sanitizados.

### 3. `coding_job_wait`
Espera de forma acotada (hasta 120s) hasta alcanzar un estado terminal o `NOTION_REVIEW`.

### 4. `coding_job_result`
Devuelve el resultado estructurado, informe de validación determinista, diagnóstico sanitizado o evidencia de promoción.

### 5. `coding_job_changes`
Devuelve el manifiesto validado de archivos modificados, agregados o eliminados con tamaños y hashes SHA-256.

### 6. `coding_job_artifact`
Devuelve el contenido completo de un archivo específico verificado dentro del worktree de la tarea.

### 7. `coding_job_request_revision`
Solicita una revisión en modo persistente desde `NOTION_REVIEW`.
- Reutiliza exactamente: `job_id`, `conversation_id`, alias del repositorio, rama de característica y worktree.
- Reanuda la sesión de Agy mediante `agy --conversation <conversation_id>`.
- Límite estricto: **máximo 3 ciclos de revisión**. La 4.ª solicitud es rechazada.
- Entrada de feedback acotada (1-4000 caracteres).

### 8. `coding_job_approve_changes`
Acción explícita requerida para aprobar cambios desde `NOTION_REVIEW`.
- Requiere `expected_validation_hash` y `expected_base_commit`.
- Rechaza evidencia desactualizada, commits base divergentes o modificaciones posteriores en el worktree.
- Transiciona a `CHANGES_APPROVED`. **No publica nada.**

### 9. `coding_job_publish_branch`
Acción explícita posterior a `CHANGES_APPROVED` que delega la publicación a un promotor independiente.
- Valida que la rama de destino no sea `main` ni la rama base protegida.
- Ejecuta push estricto sin `--force` (`refs/heads/{branch}:refs/heads/{branch}`).
- Agy no hereda credenciales de publicación.
- **Fail-closed:** Si las credenciales o configuración del promotor están ausentes, la operación falla cerrado inmediatamente.

### 10. `coding_job_create_pull_request`
Crea un Pull Request en GitHub para la rama publicada (`BRANCH_PUBLISHED`).
- Utiliza la rama de característica ya publicada y la rama base allowlisted.
- **Fail-closed:** Sin configuración de API/token, falla cerrado sin tocar GitHub.
- Transiciona a `PR_CREATED`. No realiza merge ni despliegue.

### 11. `coding_job_cancel`
Cancela de forma idempotente un trabajo activo.
- Mecanismo fijo acotado por `job_id` (sin aceptar PIDs ni comandos del usuario).
- **Preserva el worktree** intacto para análisis.
- Transiciona a `CANCELLED`.

### 12. `coding_job_cleanup`
Limpia de forma segura el worktree asignado.
- Idempotente.
- Rechaza worktrees sucios con modificaciones no confirmadas (`DirtyWorktreeError`).
- Rechaza limpieza de cambios no publicados o no aprobados (`UnpublishedChangesError`).
- Rechaza limpieza de trabajos activos (`RUNNING`, `VALIDATING`, etc.).
- Elimina únicamente el worktree y sus metadatos; **preserva intacto el repositorio bare base permanente**.
- Sin parámetro de forzado (`force`).

---

## Evidencia y validación determinista

Antes de alcanzar `NOTION_REVIEW`, el módulo `JobValidator` aplica verificaciones deterministas:
1. **Commit base:** Coincidencia exacta entre el commit base esperado y el actual.
2. **Rama:** Verificación de que la rama de característica no es `main`.
3. **Confinamiento de rutas:** Todas las rutas resuelven dentro del worktree mediante `resolve()`. Rechazo estricto de `..` y symlinks.
4. **Límites de cambios:** Máximo 20 archivos, 64 KiB por archivo, 512 KiB total y 2000 líneas de diff.
5. **Tipos de archivo permitidos:** Extensiones de texto allowlisted (`.py`, `.md`, `.toml`, `.yaml`, `.yml`, `.json`, `.sh`, `.txt`, `.html`, `.css`, `.js`, `.ts`, `Dockerfile`).
6. **Escaneo de secretos:** Detección de claves privadas, tokens de GitHub (`ghp_`, `github_pat_`), API keys de Google (`AIza`) y Bearer tokens sobre el sistema de archivos en vivo (cambios staged, unstaged y archivos no rastreados sin requerir commit).
7. **Validación de sintaxis:** AST para archivos Python y `bash -n` para scripts shell.
8. **Pruebas allowlisted:** Ejecución exclusiva de los comandos definidos en `RepositoryPolicy`. Comandos arbitrarios o provistos por el usuario son rechazados.
9. **Hash del informe de validación:** SHA-256 criptográfico generado a partir del commit base, rama, diff identity del árbol en vivo, manifiesto de archivos y resultados de pruebas.

---

## Frontera de seguridad del promotor

1. **Aislamiento de credenciales:** El contenedor y proceso de Agy jamás reciben credenciales de Git push, tokens de GitHub ni llaves SSH de publicación.
2. **Frontera de publicación:** La publicación es gestionada por `BranchPromoter` fuera del alcance de Agy.
3. **Imposibilidad de push forzado:** No existe ruta de código que invoque `--force` o refspecs `+`.
4. **Comportamiento Fail-Closed:** Ante ausencia de configuración de promotor o token en el entorno, cualquier intento de publicación o creación de PR lanza `PromotionError` y aborta.
5. **Ausencia explícita de merge y despliegue:** La integración y despliegue permanecen bajo control y aprobación humana externa.
