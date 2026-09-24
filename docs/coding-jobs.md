# Orquestación segura de trabajos de código

## Alcance

El subsistema `coding_job_*` permite que Notion IA encargue auditorías o implementaciones a Agy sin entregar al agente acceso directo a GitHub, Docker, producción o `main`.

El repositorio permitido actualmente es el alias `ia_mcp_vps` y la rama base permitida es `main`. Los repositorios privados adicionales requieren un alias y un mecanismo de lectura aislado; no deben compartir credenciales con Agy.

## Herramientas MCP expuestas

### `coding_job_create`

Crea un trabajo asíncrono y devuelve inmediatamente un `job_id` generado por el servidor.

Parámetros principales:

- `repository`: actualmente solo `ia_mcp_vps`.
- `task_type`: `audit` o `implement`.
- `goal`: objetivo acotado.
- `acceptance_criteria`: criterios verificables.
- `constraints`: restricciones adicionales.
- `base_branch`: actualmente solo `main`.

### `coding_job_status`

Devuelve estado, fase, fecha de actualización y error sanitizado.

### `coding_job_wait`

Espera de forma acotada hasta 120 segundos. La conexión MCP puede aplicar un timeout menor; para trabajos largos se debe consultar posteriormente con `coding_job_status`.

### `coding_job_result`

Devuelve resultado estructurado o diagnóstico sanitizado. Para implementaciones aprobadas por el verificador incluye:

- SHA base;
- manifiesto de archivos cambiados;
- hashes SHA-256 y tamaños;
- contenido completo de los archivos `upsert` bajo `artifacts`;
- validaciones realizadas;
- riesgos y pruebas recomendadas;
- consumo reportado por Agy.

## Estados

```text
CREATED
  → PREPARING / CONTEXT_READY
  → RUNNING
  → VERIFYING (implementación)
  → NOTION_REVIEW
```

Estados terminales de fallo: `FAILED`, `CANCELLED` y `EXPIRED`.

`NOTION_REVIEW` no significa que el cambio esté fusionado o desplegado. Significa únicamente que existe evidencia estructurada lista para revisión independiente.

## Auditoría

Para `task_type=audit`, un script fijo construye un contexto textual limitado a partir de archivos permitidos. Agy recibe el contexto dentro del prompt y tiene instrucciones de no invocar herramientas. Esto evita depender de permisos interactivos de lectura en modo headless.

Límites actuales:

- contexto máximo aproximado: 192 KiB;
- exclusión de `.git`, `.env`, credenciales, secretos y `config.yaml`;
- solo extensiones de texto allowlisted;
- salida obligatoriamente estructurada.

## Implementación aislada

Para `task_type=implement`:

1. El worker clona el `main` actual desde el repositorio permitido.
2. Registra el SHA exacto como `base_commit`.
3. Construye un contexto textual limitado.
4. Agy devuelve una propuesta estructurada con reemplazos completos o eliminaciones.
5. Un script fijo —no Agy— aplica la propuesta dentro del directorio del trabajo.
6. Se ejecutan validaciones independientes.
7. El trabajo queda en `NOTION_REVIEW` con manifiesto y artefactos.

Agy no ejecuta `git`, no hace commits, no tiene credenciales y no puede hablar con Docker.

## Límites de cambios

- máximo 20 archivos;
- máximo 64 KiB por archivo;
- máximo 512 KiB en total;
- rutas relativas sin `..`;
- no se permite `.git`;
- no se permiten symlinks como destino;
- solo archivos de texto allowlisted;
- no se permiten `.env`, bases de datos, binarios, lockfiles ni archivos generados.

## Validaciones

Antes de `NOTION_REVIEW`:

- confinamiento de rutas mediante `realpath`;
- rechazo de symlinks;
- límites de cantidad y tamaño;
- patrones de secretos conocidos;
- `git diff --check`;
- compilación sintáctica para Python cuando corresponda;
- `bash -n` para scripts shell cuando corresponda;
- manifiesto con tamaño y SHA-256.

Estas verificaciones no reemplazan la suite de pruebas específica de cada aplicación. Las pruebas de proyecto deben añadirse como comandos fijos y allowlisted, nunca como shell proporcionado por el usuario o por Agy.

## Promoción mediante GitHub MCP

Notion IA debe:

1. verificar que el SHA base siga correspondiendo al estado esperado;
2. comparar cada artefacto con criterios de aceptación;
3. ejecutar secret scanning sobre el contenido que será enviado;
4. crear una rama desde el SHA/base aprobado;
5. escribir los archivos mediante el GitHub MCP;
6. crear un Pull Request;
7. revisar checks y diff;
8. solicitar aprobación explícita al usuario;
9. fusionar usando `expectedHeadSha`;
10. desplegar únicamente la revisión fusionada.

No se permite copiar cambios directamente a `main`, usar credenciales dentro de Agy ni desplegar desde el worker.

## Eficiencia

Los contextos amplios pueden consumir decenas de miles de tokens. Los objetivos deben limitar archivos y criterios siempre que sea posible. No se deben repetir trabajos únicamente para sondeo; usar `coding_job_status`, `coding_job_wait` y resultados persistidos.
