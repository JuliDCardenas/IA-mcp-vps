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

## Contratos de Integración (Futuro N8N, JSON Schemas)

La implementación n8n no está incluida en esta entrega, pero deberá respetar estos contratos.

### 1. Payload Saliente (A N8N)
```json
{
  "request_id": "a_f2b4c6e8",
  "action": "execute_code",
  "parameters": {"command": "npm test"},
  "parameters_digest": "4a7b9c...",
  "expires_at": "2026-10-10T12:00:00.000000+00:00"
}
```
*(Nota: N8N deberá componer la UI de botones utilizando su propia configuración, pero `capability_token` nunca se transmite en este JSON para no exponerlo en histórico si no es necesario o n8n lo calculará si se incluye a futuro en una iteración permitida. Actualmente en esta entrega CORE el token se extrae del return y se pierde si no se inyecta en el JSON del webhook o requiere rediseño en entrega B).*

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
