# Propuesta de Orquestación: Integración Asíncrona de Jules mediante MCP

## 1. Objetivo y Alcance

El objetivo de esta propuesta es diseñar e implementar un subsistema ligero dentro del servidor MCP (`IA-mcp-vps`) que permita a **Notion IA** delegar tareas de programación complejas a **Jules** (agente externo) de forma asíncrona, segura y sin fricción operativa para el usuario.

A diferencia del flujo con contenedores locales aislados (`Agy`), este sistema no ejecutará código localmente, sino que actuará como un orquestador y puente de notificaciones entre Notion IA, la API del agente, una base de datos local y el usuario (vía n8n/Telegram).

### Principios
1. **Asincronía total:** Notion IA delega y recibe confirmación inmediata. No se bloquea esperando a que el código sea escrito.
2. **Registro local autónomo (SQLite):** El MCP mantiene su propia pista de auditoría sin depender de contenedores externos.
3. **Notificaciones Híbridas (Push a n8n):** El servidor MCP notifica de inmediato vía Webhooks a n8n. Para evitar que n8n deba hacer polling (lo cual es un anti-patrón en herramientas de workflow), **el propio servidor MCP se encargará de realizar un polling ligero en segundo plano** hacia la API de Jules para mantener el estado sincronizado de forma atómica.
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
- `jules_reply_to_task`:
  - **Parámetros:** `task_id` (string), `feedback_or_approval` (string).
  - **Acción:** Envía una respuesta (POST) a la sesión pausada de Jules a través de su API para reanudar el trabajo tras una solicitud de contexto o aprobación, y actualiza el estado en la BD local.

### 2.2 Base de Datos Local (SQLite)
Para mantener trazabilidad sin la complejidad de bases de datos externas en Docker, el MCP implementará una base de datos SQLite en el volumen persistente existente (`/var/lib/coding-jobs/jules_jobs.db`).

**Tabla Principal: `jules_jobs`**
- `id` (UUID primario)
- `repo_name` (varchar)
- `task_description` (text)
- `jules_agent_job_id` (varchar, ID devuelto por la API del agente)
- `status` (PENDIENTE, EN_PROGRESO, ESPERANDO_FEEDBACK, PR_CREADO, FALLIDO)
- `created_at` (timestamp)
- `updated_at` (timestamp)

### 2.3 Notificaciones y Sincronización de Estado (Background Polling en MCP)
Para garantizar consistencia y manejar escenarios de falla, pausas o finalización sin contaminar el orquestador n8n con bucles infinitos, se utilizará una estrategia de polling ligero residente en el servidor MCP:

**Fase 1: Delegación (Push MCP -> n8n)**
En el momento en que la herramienta MCP asigna la tarea con éxito, ejecuta un `POST` al webhook configurado en n8n:
```json
{
  "event": "TASK_DELEGATED",
  "task_id": "uuid-1234",
  "jules_api_id": "agent-session-888",
  "repo_name": "IA-mcp-vps"
}
```
*n8n actúa como un receptor pasivo, recibe el evento e informa al usuario por Telegram.*

**Fase 2: Polling Inteligente (MCP -> API de Jules)**
Dado que no existe un webhook push saliente oficial desde la API de Jules, **el servidor MCP asumirá el rol de monitor** mediante una tarea asíncrona en segundo plano:
- El MCP consultará la API de Jules (ej. cada 60s) *exclusivamente* para aquellos trabajos que estén en estado activo (`EN_PROGRESO` o `PENDIENTE`) en su base de datos SQLite.
- Cuando el MCP detecta un cambio de estado en la API (ej. a `WAITING_FOR_INPUT`, `COMPLETED` o `FAILED`), realiza dos acciones:
  1. Actualiza inmediatamente el registro en la base de datos local `jules_jobs.db`, manteniendo su integridad como fuente única de la verdad.
  2. Dispara un nuevo webhook push hacia n8n enviando el evento del cambio de estado, permitiendo que n8n simplemente entregue la notificación al usuario.

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

## 3.1 Flujo de Ejecución Intermedio (Feedback y Aprobación)

Es posible que Jules (el agente) necesite aclaraciones técnicas o requiera que el usuario apruebe un plan antes de generar código destructivo o realizar un PR. El sistema soporta este flujo de "ping-pong" asíncrono de la siguiente manera:

1. **Detección de la pausa:** Mediante su rutina de polling en segundo plano, el MCP descubre que la sesión en la API ha pasado a estado de espera de input. Actualiza la SQLite a `ESPERANDO_FEEDBACK`.
2. **Notificación de Pausa:** El MCP dispara un webhook a n8n enviando el contexto. n8n simplemente pasa el mensaje alertando al usuario por Telegram de que se requiere su atención.
3. **Respuesta de Notion IA:** Notion IA lee el contexto solicitado, analiza el proyecto y, basándose en el conocimiento del usuario, utiliza la herramienta `jules_reply_to_task` pasándole la instrucción o aprobación (ej. *"Aprobado, procede con el plan 2"*).
4. **Reanudación:** El MCP hace un llamado POST a la API de Jules entregando la respuesta. Jules reanuda la ejecución en la nube.

---

## 4. Requisitos de Infraestructura y Configuración

Se añadirán las siguientes claves de configuración al sistema, administradas mediante variables de entorno seguras (no versionadas):

- `JULES_API_KEY`: Autenticación para la plataforma del agente.
- `JULES_API_URL`: Endpoint oficial de la plataforma (ej. `https://jules.googleapis.com/v1alpha/...`).
- `N8N_WEBHOOK_URL`: Endpoint del webhook HTTP creado en el flujo de n8n para recibir alertas de delegación.

No se requerirá despliegue de nuevos contenedores. La librería nativa de Python `sqlite3` será suficiente para gestionar la persistencia local en el volumen del MCP.

---

## 5. Decisiones Aprobadas
- **Monitoreo Liderado por MCP (No Outbox local complejo):** Se aprueba la eliminación de sistemas pesados de colas (outbox local). El propio servidor MCP se encargará del polling ligero de sesiones activas, garantizando que su SQLite sea la única fuente de la verdad, mientras que n8n permanecerá como un sistema pasivo de recepción de webhooks (push).
- **Independencia de Agy:** Las herramientas y persistencia de Jules vivirán en paralelo sin afectar la lógica o seguridad restrictiva de Agy.
- **Aprobación final manual (PR):** El sistema MCP NO realizará *merges* ni despliegues a producción; Jules entregará ramas/PRs, respetando la frontera de seguridad del proyecto.
