# Propuesta de Orquestación: Integración Asíncrona de Jules mediante MCP

## 1. Objetivo y Alcance

El objetivo de esta propuesta es diseñar e implementar un subsistema ligero dentro del servidor MCP (`IA-mcp-vps`) que permita a **Notion IA** delegar tareas de programación complejas a **Jules** (agente externo) de forma asíncrona, segura y sin fricción operativa para el usuario.

A diferencia del flujo con contenedores locales aislados (`Agy`), este sistema no ejecutará código localmente, sino que actuará como un orquestador y puente de notificaciones entre Notion IA, la API del agente, una base de datos local y el usuario (vía n8n/Telegram).

### Principios
1. **Asincronía total:** Notion IA delega y recibe confirmación inmediata. No se bloquea esperando a que el código sea escrito.
2. **Registro local autónomo (SQLite):** El MCP mantiene su propia pista de auditoría sin depender de contenedores externos.
3. **Notificaciones Push:** Cero *polling*. El MCP notifica directamente vía Webhooks, y el ciclo se cierra mediante eventos de GitHub.
4. **Desacoplamiento:** Herramientas separadas e independientes del flujo heredado de Agy.

---

## 2. Arquitectura y Componentes

### 2.1 Herramientas MCP
Se crearán nuevas herramientas exclusivas bajo un módulo dedicado (`src/dari_mcp_vps/tools/jules_tools.py`) para aislar los contratos.

- `jules_request_coding_task`:
  - **Parámetros:** `repo_name` (string), `task_description` (string, detallada).
  - **Acción:** Realiza un POST a la API de Jules, genera un ID único, inserta el registro en la BD SQLite local, lanza un webhook a n8n, y devuelve a Notion un mensaje de éxito inmediato.
- `jules_check_task_status`:
  - **Parámetros:** `task_id` (string).
  - **Acción:** Consulta la BD SQLite local y devuelve el estado actual y el historial del trabajo. (Nota: el estado final será validado externamente).

### 2.2 Base de Datos Local (SQLite)
Para mantener trazabilidad sin la complejidad de bases de datos externas en Docker, el MCP implementará una base de datos SQLite en el volumen persistente existente (`/var/lib/coding-jobs/jules_jobs.db`).

**Tabla Principal: `jules_jobs`**
- `id` (UUID primario)
- `repo_name` (varchar)
- `task_description` (text)
- `jules_agent_job_id` (varchar, ID devuelto por la API del agente)
- `status` (PENDIENTE, EN_PROGRESO, PR_CREADO, FALLIDO)
- `created_at` (timestamp)
- `updated_at` (timestamp)

### 2.3 Notificaciones (Webhook + n8n)
La arquitectura de notificaciones se divide en dos fases push (sin polling):

**Fase 1: Delegación (MCP -> n8n)**
En el momento exacto en que la herramienta MCP asigna la tarea con éxito, ejecuta un `POST` al webhook configurado en n8n enviando:
```json
{
  "event": "TASK_DELEGATED",
  "task_id": "uuid-1234",
  "repo_name": "IA-mcp-vps",
  "description": "Corregir vulnerabilidad en auth..."
}
```
*n8n recibe esto y alerta por Telegram al usuario: "Jules comenzó a trabajar en IA-mcp-vps".*

**Fase 2: Finalización (GitHub -> n8n)**
Dado que Jules entregará su trabajo directamente como un Pull Request (PR) en el repositorio objetivo, el cierre del ciclo no es responsabilidad del MCP.
n8n se configurará para escuchar eventos de *New Pull Request* del repositorio a través de la API de GitHub (o webhook nativo de GitHub).
*n8n recibe el evento del PR y alerta por Telegram al usuario: "Jules ha terminado y dejó un PR listo para revisar".*

---

## 3. Flujo de Ejecución (Golden Path)

1. **Análisis:** Notion IA inspecciona el repositorio utilizando sus capacidades actuales de lectura. Identifica que es necesario refactorizar un módulo.
2. **Delegación:** Notion IA invoca `jules_request_coding_task(repo='mi-repo', task='Refactorizar módulo X')`.
3. **Orquestación en MCP (Ejecución paralela):**
   - El código en Python llama a la API de Jules y obtiene confirmación (ID remoto).
   - El código inserta una nueva fila en la base de datos `jules_jobs.db`.
   - El código lanza un `POST` al webhook de n8n.
   - El código retorna a Notion IA: `"Tarea delegada con éxito (ID: ...). No es necesario esperar."`
4. **Notificación de Inicio:** n8n envía el mensaje inicial por Telegram al usuario.
5. **Trabajo Autónomo:** Jules (en la nube) clona el repositorio, aplica los cambios, corre pruebas y crea el Pull Request.
6. **Notificación de Cierre:** El PR activa a n8n, el cual envía el mensaje de Telegram indicando que el trabajo está listo para revisión manual y despliegue.

---

## 4. Requisitos de Infraestructura y Configuración

Se añadirán las siguientes claves de configuración al sistema, administradas mediante variables de entorno seguras (no versionadas):

- `JULES_API_KEY`: Autenticación para la plataforma del agente.
- `JULES_API_URL`: Endpoint de la plataforma de Jules (ej. `https://api.agency.ai/v1/agents/...`).
- `N8N_WEBHOOK_URL`: Endpoint del webhook HTTP creado en el flujo de n8n para recibir alertas de delegación.

No se requerirá despliegue de nuevos contenedores. La librería nativa de Python `sqlite3` será suficiente para gestionar la persistencia local en el volumen del MCP.

---

## 5. Decisiones Aprobadas
- **Descarte del modelo Outbox/Polling:** Se aprueba eliminar las complejas colas de mensajería (outbox local) en favor de llamadas directas y eventos de GitHub, salvaguardando los recursos del VPS y de la cuota de la IA.
- **Independencia de Agy:** Las herramientas y persistencia de Jules vivirán en paralelo sin afectar la lógica o seguridad restrictiva de Agy.
- **Aprobación final manual (PR):** El sistema MCP NO realizará *merges* ni despliegues a producción; Jules entregará ramas/PRs, respetando la frontera de seguridad del proyecto.
