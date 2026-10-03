# Propuesta: Jules como ejecutor asíncrono y avisos mediante n8n

- **Estado:** borrador para discusión; no autoriza implementación ni despliegue.
- **Fecha:** 2026-10-03 (America/Bogota).
- **Participantes:** Julián (decisión final), Notion IA/Dari (propuesta y revisión), Jules (revisión técnica).
- **Repositorio:** `JuliDCardenas/IA-mcp-vps`.
- **Contexto:** [Orquestador de desarrollo en Notion](https://www.notion.so/ff9f11c7c8f2468cb2f1a849e76760ca).
- **Contratos existentes:** [coding-jobs.md](coding-jobs.md).

> Este documento reúne la idea para pulirla entre Julián, Notion IA y Jules. Las interfaces, estados y decisiones técnicas descritas como propuestas todavía no existen ni están aprobadas. No cambiar código hasta cerrar las decisiones que bloqueen el primer corte.

## 1. Problema y resultado deseado

Programar puede tomar varios minutos. Una llamada MCP no debe permanecer abierta durante toda la ejecución ni consumir ciclos de Notion IA consultando continuamente.

Queremos que Julián pueda pedir un cambio desde Notion; el MCP lo envíe a Jules y devuelva rápidamente un identificador; el encargo quede registrado de forma persistente; y n8n avise por Telegram cuando Jules necesite intervención o entregue un resultado. Después, Notion IA puede recuperar el trabajo para revisar la evidencia.

**Enviado no significa terminado. Terminado no significa validado. PR creado no significa aprobado ni fusionado.**

## 2. Base conocida y límites de la evidencia

### Repositorio actual

Se revisaron `README.md` y `docs/coding-jobs.md` en `main`, commit `e0364dd2803adfca6712561d933c15340d5fc4e1`. Documentan trabajos asíncronos Agy, revisión, validación y promoción separada. Este borrador no implica auditoría completa del código ni verificación del despliegue actual. Antes de implementar, contrastar los contratos documentados con el código y pruebas vigentes.

No reemplazar Agy ni modificar sus contratos existentes como parte de este borrador.

### API oficial de Jules

Según la documentación consultada:

- Existe una API REST en `https://jules.googleapis.com/v1alpha` y está marcada como alpha.
- La autenticación usa una API key creada en los ajustes de Jules, enviada en `X-Goog-Api-Key`.
- Los repositorios se conectan mediante la GitHub App de Jules y se descubren como `sources`.
- Se pueden crear sesiones, consultar su estado y actividades, aprobar planes y enviar mensajes a sesiones activas.
- `requirePlanApproval: true` exige aprobación del plan. Sin ese campo, los planes se aprueban automáticamente.
- `automationMode: "AUTO_CREATE_PR"` permite crear PR automáticamente. Si se omite, no se crea un PR automáticamente.
- Una sesión puede esperar aprobación, pedir feedback, estar pausada, terminar o fallar.

Fuentes oficiales:

- [Jules API: autenticación, sources, sesiones y actividades](https://developers.google.com/jules/api).
- [Referencia de sesiones y estados](https://jules.google/docs/api/reference/sessions/).

**No verificado:** disponibilidad y límites en la cuenta de Julián, garantías de idempotencia al crear sesiones, webhooks de eventos, semántica de cancelación, entrega de PR como borrador, revisión después de completar una sesión y completitud de los artefactos. No asumir estas capacidades. Revalidar documentación y comportamiento antes de implementarlas.

El soporte de Jules para consumir MCP de terceros es una integración distinta: aquí nuestro MCP actúa como cliente de la API de Jules.

## 3. Alcance y fuera de alcance

### Alcance propuesto

1. Añadir un ejecutor Jules sin romper el flujo Agy.
2. Crear encargos asíncronos con repositorio y rama explícitos.
3. Persistir identidad, estado, referencias y eventos del trabajo.
4. Recuperar planes, solicitudes de feedback y resultados disponibles.
5. Usar n8n para seguimiento periódico y entrega de avisos por Telegram.
6. Conservar revisión independiente y aprobación humana de merge/despliegue.

### Fuera de alcance inicial

- Sustituir Agy o rediseñar todo el orquestador.
- Automatizar navegador, cookies o sesiones web de Google.
- Permitir shell libre, comandos arbitrarios o acceso de Jules al VPS.
- Merge o despliegue automáticos.
- Aprobar planes o cambios desde botones de Telegram.
- Importar repositorios completos o logs ilimitados a Notion.
- Suponer que Notion IA se reactiva automáticamente al recibir una notificación.
- Crear una segunda BD en Notion como requisito del MVP. Una vista allí sería opcional y posterior.

## 4. Lenguaje compartido

| Término | Significado |
| --- | --- |
| Encargo / job | Registro local que representa la solicitud y su seguimiento |
| Sesión Jules | Unidad de trabajo remota identificada por el proveedor |
| Estado remoto | Estado reportado por Jules, conservado sin reinterpretación destructiva |
| Estado local | Estado de envío, seguimiento o revisión del orquestador |
| Evento | Novedad persistida: plan listo, feedback solicitado, resultado o error |
| Aviso | Mensaje al usuario derivado de un evento; no constituye aprobación |
| Resultado | Evidencia y referencias obtenidas del proveedor; requiere revisión |

## 5. Arquitectura propuesta

```text
Julián → Notion IA → herramientas MCP
                           ↓
                   Registro persistente
                           ↓
                    Adaptador de Jules
                           ↓ API REST
                  Sesión remota de Jules

n8n (programación periódica)
  → interfaz autenticada de seguimiento del MCP
  → MCP consulta sesiones/actividades de Jules
  → MCP actualiza registro y genera eventos/avisos pendientes
  → n8n entrega avisos a Telegram
  → n8n confirma entrega al MCP

Julián vuelve a Notion
  → Notion recupera el job
  → revisa plan/resultado/PR
  → solicita feedback o aprobación según política
```

**Preferencia inicial, pendiente de acuerdo:** el MCP conserva la API key de Jules y habla con su API; n8n solo recibe permisos acotados de seguimiento y notificación. No dar acceso directo de n8n a toda la BD ni usar credenciales de administración del VPS.

n8n es responsable de la programación y entrega; el MCP es responsable de la identidad del job, normalización, persistencia y autorización. No introducir simultáneamente dos procesos que consulten las mismas sesiones sin coordinación.

## 6. Flujo de envío y respuesta rápida

1. Validar permisos, repositorio conectado, rama y política de ejecución.
2. Recibir o generar una clave de idempotencia estable para esa solicitud.
3. Persistir el encargo local antes de hacer la llamada remota.
4. Crear la sesión Jules con timeout HTTP acotado; no esperar a la programación.
5. Persistir `provider_session_id`, nombre del recurso, URL y estado remoto recibido.
6. Devolver el job a Notion IA.

Ejemplo **propuesto**, no contrato existente:

```json
{
  "job_id": "job_example",
  "executor": "jules",
  "submission_status": "accepted",
  "provider_session_id": "session_example",
  "provider_state": "QUEUED",
  "require_plan_approval": true,
  "auto_create_pr": false,
  "message": "Jules aceptó el encargo. Se notificará cuando necesite intervención o haya un resultado."
}
```

La promesa de notificación solo debe mostrarse si el seguimiento está habilitado y saludable.

**Caso crítico: timeout ambiguo.** Si Jules pudo haber creado la sesión, pero no recibimos o persistimos la respuesta, marcar `submission_unknown`. No informar éxito ni volver a crear ciegamente otra sesión. La clave local evita reintentos duplicados dentro de nuestro servicio, pero por sí sola no garantiza idempotencia remota. Definir reconciliación o intervención humana tras verificar qué ofrece la API.

Si optamos por encolar el envío en lugar de hacer la petición HTTP dentro de la llamada MCP, devolver `queued_for_submission`, no `accepted`. La respuesta debe distinguir encargo local aceptado y sesión remota confirmada.

## 7. Registro persistente mínimo

Preferir una BD transaccional sobre un archivo de logs como fuente operativa. Reutilizar almacenamiento existente si satisface concurrencia, migraciones y recuperación; elegir SQLite o PostgreSQL después de inspeccionarlo. Los logs sanitizados complementan la BD, no la sustituyen.

### Encargos

- `job_id`, ejecutor, solicitante autorizado y clave de idempotencia.
- Título, repositorio canónico, source remoto y rama base.
- Objetivo, criterios y restricciones con tamaños máximos y control de acceso.
- Estado local de envío y estado remoto original.
- ID/nombre/URL de sesión; referencias de PR y evidencia si existen.
- Política: aprobación de plan y PR automático.
- Fechas de creación, actualización, último seguimiento y próximo intento.
- Cursor o identificadores de actividades procesadas.
- Error sanitizado, cantidad de reintentos y señal de seguimiento degradado.

### Eventos y cola de avisos (outbox)

- `event_id`, `job_id`, tipo, actividad o versión de origen y fecha.
- Clave única de deduplicación por evento y destinatario.
- Estado del aviso: pendiente, reservado, enviado o agotado.
- Intentos, próximo reintento, vencimiento de reserva y error sanitizado.
- Confirmación/identificador de mensaje Telegram cuando exista.

Persistir evento y aviso pendiente en una misma transacción. Conservar cambios de estado y también nuevas actividades relevantes cuando el estado remoto no cambie.

No guardar API keys, tokens de bot ni cabeceras de autenticación en estas tablas o logs. Definir retención, backup, restauración y política de borrado antes de producción.

## 8. Contratos por acordar

Elegir entre ampliar `coding_job_*` con `executor` o añadir una familia Jules separada. No añadir parámetros a contratos públicos sin revisar compatibilidad y pruebas.

Operaciones necesarias, sin fijar todavía nombres finales:

- Crear un encargo.
- Consultar estado y resultado.
- Recuperar el plan y solicitar su aprobación explícita.
- Enviar feedback a la sesión activa.
- Listar trabajos que necesitan seguimiento y refrescarlos en lotes acotados.
- Reservar avisos pendientes y confirmar entrega o fallo.

La interfaz de n8n podría ser HTTP autenticado con operaciones fijas. Si se elige MCP, verificar previamente el soporte real del cliente n8n. No exponer endpoints genéricos para ejecutar herramientas o SQL arbitrario.

Mantener separados `COMPLETED` remoto, evidencia disponible, validación local y aprobación final. No fabricar un hash de validación ni declarar pruebas verificadas a partir de un resumen del agente.

## 9. Seguimiento n8n y Telegram

### Disparador y consultas

- Inicio propuesto: cada 60–120 segundos; intervalo configurable, por validar contra cuota y volumen.
- Consultar solo trabajos que lo requieran, con lotes, paginación y exclusión/reserva para ejecuciones concurrentes.
- Aplicar backoff con jitter ante 429, errores transitorios o indisponibilidad.
- Respetar límites de la cuenta y `Retry-After` si está disponible.
- Un error de consulta no significa que la programación haya fallado: registrar degradación del seguimiento por separado.
- Comprobar actividades y resultado final antes de cerrar seguimiento. Para pausas o esperas prolongadas, definir una frecuencia menor y un límite de intervención.

### Eventos notificables por defecto

| Evento | Aviso esperado |
| --- | --- |
| Plan pendiente de aprobación | Abrir sesión y revisar plan |
| Feedback requerido | Resumen sanitizado y enlace para responder |
| Completado | Resultado disponible, enlace a sesión y PR si existe |
| Fallo remoto | Motivo disponible y siguiente paso |
| Seguimiento degradado sostenido | No se pudo comprobar el estado; no afirmar fallo de Jules |

No avisar por cada línea de progreso. No mandar aprobaciones, código completo ni secretos en Telegram. El texto debe usar formato seguro y escapar contenido no confiable.

Ejemplo:

```text
Jules terminó: corregir exportación PDF
Repositorio: JuliDCardenas/InformesMediabase
Encargo: job_example
Resultado: disponible para revisión; todavía no aprobado
Sesión: <enlace devuelto por Jules>
PR: <enlace, solo si existe>
Siguiente paso: volver a Notion y pedir revisar el encargo.
```

### Garantías de entrega realistas

Marcar enviado únicamente después de una respuesta de éxito de Telegram. Reservar el aviso con un lease para evitar carreras entre ejecuciones de n8n. Reintentar fallos y alertar si se agotan intentos.

Hay una ventana inevitable si Telegram recibe el mensaje y el proceso cae antes de guardar la confirmación: el reintento puede duplicarlo. No prometer entrega exactamente una vez. Objetivo: entrega al menos una vez con deduplicación interna, identificador visible y duplicados excepcionales documentados.

## 10. Seguridad y límites

- Guardar secretos mediante el mecanismo seguro del despliegue; nunca en Markdown, commits o Notion.
- Autorizar solo repositorios expresamente conectados y permitidos; no habilitar toda la cuenta por defecto.
- Jules usa su propia GitHub App: el broker de tokens de nuestra V2 no sustituye esa autorización.
- El código se procesa en un servicio externo. Confirmar permisos, privacidad y repositorios aptos antes de enviarlo.
- Los límites locales de Docker, red, shell y worktree de Agy no se transfieren automáticamente a Jules. Un prompt no es una frontera de seguridad.
- Tratar repositorios, respuestas y actividades como datos no confiables: limitar tamaño, sanear enlaces y no ejecutar instrucciones embebidas.
- Separar credencial de seguimiento de n8n y credenciales capaces de crear encargos o aprobar planes.
- Telegram solo informa; merge y despliegue siguen requiriendo aprobación explícita y revisión del SHA vigente.
- La API alpha queda detrás de un adaptador, con timeouts, errores tipados y posibilidad de deshabilitar Jules sin afectar Agy.
- No equiparar borrar una sesión con detener su ejecución. Cancelación y limpieza requieren verificar las garantías del proveedor.

## 11. Decisiones pendientes

Las preferencias de esta tabla son propuestas, no acuerdos.

| ID | Decisión | Preferencia inicial | Responsable del acuerdo |
| --- | --- | --- | --- |
| D1 | Integración de herramientas | `executor` si resulta compatible; familia separada si obliga a fingir capacidades Agy | Julián + revisión técnica |
| D2 | Persistencia | Reutilizar BD viable; no introducir otra sin necesidad | Revisión del repositorio |
| D3 | Dueño del seguimiento | n8n programa; MCP consulta y persiste | Julián |
| D4 | PR automático | Desactivado en primer piloto; decidir antes del corte de entrega | Julián |
| D5 | Aprobación del plan | Obligatoria; primer piloto con aprobación humana explícita | Julián |
| D6 | Avisos y frecuencia | Intervención, resultado y fallo; polling 60–120 s sujeto a cuota | Julián |
| D7 | Timeout ambiguo | No reenvío ciego; reconciliación o intervención | Jules + revisión técnica |
| D8 | Repositorio piloto | Repo de prueba sin secretos ni impacto productivo | Julián |
| D9 | Revisiones tras completado | No asumir reanudación; verificar si exige nueva sesión | Jules + evidencia API |
| D10 | Retención y destino Telegram | Chat permitido, datos mínimos y retención explícita | Julián |

**Bloquean el primer corte:** D1, D2, D7, selección/autorización del piloto y disponibilidad real de API en la cuenta. El cierre del circuito PR/notificación depende además de D3–D6 y D10.

## 12. Cortes verticales propuestos

No son tickets creados ni compromisos de ejecución.

1. **Encargo recuperable:** crear una sesión de prueba, persistirla y recuperar su estado tras reinicio. Incluye reintento local y timeout ambiguo. Depende de los bloqueos del primer corte.
2. **Intervención humana:** detectar un plan o solicitud de feedback, recuperarlo y responder por una operación autorizada. Depende del corte 1 y de D5.
3. **Aviso extremo a extremo:** n8n sigue un encargo real y entrega un aviso Telegram; probar caída y recuperación del outbox. Depende de cortes 1–2, D3, D6 y D10.
4. **Entrega revisable:** recuperar cambios/PR, validar independientemente y revisar sin merge ni despliegue. Depende de los cortes anteriores, D4 y garantías de evidencia/revisión del proveedor.

## 13. Criterios de aceptación para una futura implementación

- [ ] La llamada de creación no espera a que termine la programación.
- [ ] Solo se informa aceptación remota cuando la sesión está confirmada y persistida.
- [ ] Un reintento con la misma clave devuelve el mismo job; un timeout ambiguo no provoca reenvío automático.
- [ ] El job se recupera tras reiniciar MCP o n8n.
- [ ] Agy conserva su comportamiento y pruebas previas.
- [ ] Planes y feedback pendientes se distinguen de éxito y fallo.
- [ ] El seguimiento procesa nuevas actividades relevantes incluso sin cambio de estado.
- [ ] n8n continúa sin que el chat de Notion esté abierto.
- [ ] Dos ejecuciones concurrentes no reservan el mismo aviso simultáneamente.
- [ ] Fallos de Telegram conservan el aviso pendiente; duplicados por caída tras envío quedan documentados.
- [ ] 429 y errores temporales aplican backoff sin declarar fallo de la sesión.
- [ ] Un resultado completado sin PR produce un aviso correcto, sin enlace inventado.
- [ ] No aparecen secretos ni logs sin límite en BD, avisos o respuestas MCP.
- [ ] Notificar no aprueba planes, cambios, merge ni despliegue.
- [ ] No se declara evidencia del agente como validación independiente.

Validación futura: pruebas unitarias de mapeo y deduplicación; pruebas de integración con proveedor simulado para timeout, concurrencia y caídas; y un smoke test real autorizado en el piloto. No se requieren pruebas de ejecución para este cambio exclusivamente documental.

## 14. Revisión y registro de acuerdos

Usar comentarios del PR para discutir frases concretas. Para recomendaciones extensas, añadir una entrada aquí. Conservar la diferencia entre propuesta, evidencia y decisión; no convertir recomendaciones de Jules en hechos verificados automáticamente.

### Recomendaciones de Julián

- Pendientes.

### Recomendaciones de Jules

#### Propuesta: Integración Ligera y Asíncrona (Webhook + GitHub PR Trigger)
Tras analizar la propuesta inicial, recomiendo adoptar una arquitectura simplificada que evite la complejidad de mantener colas (outbox) locales, bases de datos o rutinas de polling constante (cada 60s) que podrían sobrecargar el servidor MCP o agotar cuotas.

1. **Herramientas MCP Separadas:**
   - Crear un módulo independiente (ej. `jules_tools.py`) para evitar acoplamientos y romper los contratos rígidos del sistema `Agy` existente.
   - Herramienta `jules_request_coding_task`: Notion IA le pasa el nombre del repositorio y la instrucción detallada. Esta herramienta invoca de manera asíncrona la API de Jules y devuelve a Notion un acuse de recibo inmediato (evitando bloqueos o timeouts costosos para Notion IA).

2. **Notificaciones Push vs Polling:**
   - En lugar de que n8n deba hacer polling local para saber si hay nuevas tareas, el MCP ejecutará un **Webhook (POST)** directamente a n8n en el momento en que se delegue la tarea, enviando los detalles de la solicitud.
   - Esto desencadenará inmediatamente el mensaje en Telegram: *"Nueva tarea asignada a Jules: <descripción>"*.

3. **Cierre de Ciclo vía GitHub (El resultado):**
   - Dado que Jules entrega los resultados en forma de un Pull Request en el repositorio, recomiendo configurar n8n para escuchar directamente los eventos de *New Pull Request* de GitHub en lugar de consultar constantemente el estado a través del MCP.
   - Cuando n8n detecte el PR de Jules, enviará la notificación final a Telegram: *"Jules ha terminado y dejó un PR listo para revisar"*.

4. **Ventajas:**
   - **Simplicidad:** Reduce la cantidad de código y estados a mantener dentro del MCP.
   - **Cero bloqueos:** Notion despacha y sigue con sus tareas; n8n recibe eventos solo cuando las cosas pasan (Webhooks).
   - **Resiliencia:** Si el MCP falla después de despachar, GitHub sigue siendo la fuente de verdad del trabajo terminado.

### Respuesta de Notion IA / revisión técnica

- Pendiente de comentarios.

### Acuerdos

| Fecha | Decisión | Justificación / evidencia | Aprobación de Julián |
| --- | --- | --- | --- |
| — | Sin acuerdos finales todavía | Borrador inicial | Pendiente |

### Condición para empezar

Julián aprueba el alcance del primer corte y sus decisiones bloqueantes; se contrasta con código/pruebas vigentes y capacidades reales de Jules; se fija un criterio de cierre y reversión. A partir de ese acuerdo se crean los tickets necesarios. Este documento por sí solo no autoriza credenciales, ejecución remota, cambios de código, merge ni despliegue.
