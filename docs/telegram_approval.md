# Sistema de Aprobación por Telegram (Simulación CORE)

Este documento describe la arquitectura central de almacenamiento y seguridad del sistema de aprobación duradera.

## Características de Seguridad (Entregable A)
1. **Peticiones Inmutables**: Digest criptográfico que enlaza `action` + `parameters` + `version`.
2. **Capacidad Opaca Segura**: Los tokens opacos de decisión se calculan utilizando `HMAC(request_id, environment_secret)` en tiempo de ejecución.
   - **Ningún token o secreto en texto plano se persiste jamás en el Outbox de base de datos** para prevenir fugas.
3. **Determinismo Atómico**: Reclamos concurrentes son prevenidos utilizando `UPDATE...RETURNING` a nivel de SQLite en exclusión mutua.
4. **Resiliencia de Red**: Entregas salientes utilizan `HTTPS` (estricto) y bloquean `redirects`. Incluyen control duradero de retries y backoff logarítmico.
5. **Switch Off por Defecto**: Todo el feature está deshabilitado a menos que explicitamente se establezca `APPROVAL_ENABLED=true` y se validen las variables mandatorias (`HTTPS`, secretos no vacíos, identificadores exactos).
6. **Límites Estrictos de Payload (Telegram InlineKeyboard)**: El esquema de ID corto y token base64/urlsafe garantiza que el string en n8n como callback final sea `<= 64 bytes`.

## Configuración y Variables de Entorno

- `APPROVAL_ENABLED`: `true` o `false` (Defecto: `false`).
- `APPROVAL_DB_PATH`: `/var/lib/coding-jobs/approvals.db`
- `APPROVAL_N8N_WEBHOOK_URL`: `https://[tu-n8n]/webhook/approval-request` (HTTPS estrictamente obligatorio).
- `APPROVAL_N8N_WEBHOOK_KEY`: Token estático para la llamada de MCP a n8n.
- `APPROVAL_WEBHOOK_SECRET`: Secreto utilizado para derivar capacidades inmutables HMAC y autenticar respuestas entrantes de n8n hacia `/webhook/approval-decision`.
- `APPROVAL_TELEGRAM_USER_ID`: Tu ID numérico de Telegram (solo tú puedes aprobar).
- `APPROVAL_TELEGRAM_CHAT_ID`: ID del chat donde opera el bot.

## Contratos de Integración N8N

El webhook n8n utilizará nativamente la funcionalidad **"Telegram Send and Wait for Response"**. El MCP enviará un token efímero que n8n conservará en memoria y devolverá en su flujo de continuación (ej. Approve Within Chat).

*No es necesario* ensamblar un `callback_data` propio de Telegram ni entregarle a n8n el secreto maestro del servidor. N8n solo pasa los datos.

### 1. Payload Saliente (A N8N)
```json
{
  "request_id": "a_f2b4c6e8",
  "action": "approval_demo",
  "parameters": {"target": "demo"},
  "parameters_digest": "4a7b9c...",
  "expires_at": "2026-10-10T12:00:00.000000+00:00",
  "capability_token": "hmac_token_24_chars"
}
```
*(Nota: El `capability_token` se genera dinámicamente en el envío HTTP y NUNCA se persiste en texto plano en la base de datos local).*

### 2. Payload Entrante (Desde N8N a MCP)
Endpoint: `POST /webhook/approval-decision`
Auth: `Authorization: Bearer <APPROVAL_WEBHOOK_SECRET>`
```json
{
  "user_id": 111111111,
  "chat_id": 222222222,
  "request_id": "a_f2b4c6e8",
  "capability_token": "hmac_token_24_chars",
  "decision": "APPROVED"
}
```
