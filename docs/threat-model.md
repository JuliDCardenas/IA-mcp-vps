# Modelo de amenazas

## Activos protegidos

- VPS y socket de Docker;
- credenciales de GitHub, llaves SSH y tokens de acceso;
- repositorios Git y rama protegida `main`;
- repositorios base permanentes y worktrees de trabajo;
- datos y configuración de producción;
- integridad del historial de revisiones y registros de auditoría.

---

## Riesgos principales y mitigaciones

### 1. Escritura directa o accidental en `main`
- **Mitigación:** Todas las implementaciones generan ramas de características dedicadas (`feat/<alias>-<work_item_id>`). El orquestador y el validador rechazan explícitamente cualquier rama que coincida con `main` o la rama base. La publicación y PR se dirigen a ramas secundarias. **No existe ruta de código para merge directo ni despliegue automático.**

### 2. Exfiltración de secretos o credenciales
- **Mitigación:**
  - El entorno de Agy no recibe tokens de GitHub ni llaves SSH de publicación.
  - La publicación se gestiona mediante un componente independiente (`BranchPromoter`).
  - Escaneo automático mediante expresiones regulares antes de aprobar o publicar cambios (detecta `ghp_`, `AIza`, claves privadas, Bearer tokens).
  - Los errores y salidas de consola son redactados y truncados a un tamaño acotado.

### 3. Prompt injection y manipulación desde repositorios no confiables
- **Mitigación:** El contenido del repositorio se trata como datos pasivos no confiables. No se permite que instrucciones halladas en archivos modifiquen el flujo de orquestación, alteren comandos de prueba o eludan validaciones.

### 4. Ejecución de comandos arbitrarios
- **Mitigación:** Las pruebas automáticas se ejecutan exclusivamente a partir de la tupla fija `test_commands` definida en `RepositoryPolicy`. Ningún comando provisto por el usuario, por el repositorio o por Agy es aceptado. Todas las llamadas a `subprocess.run` usan listas de argumentos con `shell=False`.

### 5. Escape por rutas relativas, symlinks o traversal
- **Mitigación:**
  - Confinamiento estricto bajo `storage_root` usando `resolve().relative_to(...)`.
  - Validación de identificadores con `SAFE_ID_PATTERN` (`^[A-Za-z0-9_-]{1,64}$`), rechazando `.`, `..` y separadores de ruta.
  - Prohibición explícita de enlaces simbólicos (`os.path.islink`) tanto en worktrees como en archivos cambiados.

### 6. Pérdida o corrupción de trabajo por colisiones o carreras
- **Mitigación:**
  - Escritura atómica de metadatos JSON (`_atomic_write_json`) usando identificadores de archivo resistentes a colisiones (`uuid4()`).
  - Verificación estricta de fast-forward (`git merge-base --is-ancestor`) en refrescos de rama base.
  - Reutilización idempotente de ramas y worktrees para revisiones sucesivas del mismo ítem de trabajo.

### 7. Limpieza destructiva de trabajo no publicado o sucio
- **Mitigación:**
  - `coding_job_cleanup` ejecuta `git status --porcelain=v1` y rechaza la limpieza si existen cambios sin confirmar (`DirtyWorktreeError`).
  - Rechaza la limpieza si los cambios no han sido aprobados y promovidos a PR (`UnpublishedChangesError`).
  - No existe parámetro `force` para omitir estas salvaguardas.
  - El repositorio bare base permanente nunca se elimina durante la limpieza de un worktree.

### 8. Comportamiento en ausencia de configuración (Fail-Closed)
- **Mitigación:** Si las credenciales o configuración del promotor o de la API de GitHub no están presentes, `publish_branch` y `create_pull_request` fallan cerrado inmediatamente con `PromotionError`, impidiendo cualquier intento de comunicación insegura o degradación silenciosa.

---

## Separación estricta de responsabilidades

```text
[Agy / Implementador]
      ↓ (Solo escribe en su worktree asignado)
[JobValidator]
      ↓ (Valida commit base, diff, tipos, secretos, pruebas allowlisted -> report_hash)
[Revisión Humana / Notion]
      ↓ (Aprobación explícita con hash y commit base exactos)
[BranchPromoter] (Componente independiente con credenciales)
      ↓ (Push seguro sin --force)
[GitHub PR Client]
      ↓ (Abre PR para revisión y merge externo)
```
