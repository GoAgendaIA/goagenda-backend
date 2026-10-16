# GoAgenda Backend

API en FastAPI para GoAgenda. El corazon del producto es un agente de IA (LangGraph) que agenda, consulta, cancela y reprograma citas conversando por un chat web publico y aislado por negocio: cada negocio tiene su propio enlace de chat (`/chat/<business_id>/...`) que su dueño comparte con sus clientes. WhatsApp (Meta Cloud API oficial, o un numero vinculado via un microservicio Baileys aparte) se usa solo para envios salientes: recordatorios de citas y confirmaciones automaticas de reservas hechas en la web.

Proyecto 100% Python. No requiere Node ni npm para correr.

## Requisitos

- Python 3.12+
- Un proyecto de Supabase (base de datos + auth), con acceso a su conexion Postgres directa (no solo la REST API)
- Una API key de OpenAI
- Credenciales de Firebase (para las notificaciones push), opcional en desarrollo

## Instalacion

```bash
# 1. Clonar el repo y entrar a la carpeta
git clone <url-del-repo>
cd goagenda-backend

# 2. Crear y activar un entorno virtual (Python 3.12)
python3.12 -m venv venv
source venv/bin/activate   # en Windows: venv\Scripts\activate

# 3. Instalar dependencias
python -m pip install -r requirements.txt

# 4. Configurar variables de entorno
cp .env.example .env
# luego edita .env y completa los valores reales
```

### DATABASE_URL (checkpointer del agente de chat)

El agente de chat usa el checkpointer `PostgresSaver` de LangGraph para persistir el contexto de cada conversacion (sobrevive reinicios del backend y funciona con varias instancias corriendo a la vez). Necesita una conexion Postgres directa, no la REST API de Supabase:

1. Supabase Dashboard → tu proyecto → **Project Settings → Database → Connection string**.
2. Usa la pestaña **Session pooler** (puerto `5432`), **no** "Direct connection" (host `db.<project-ref>.supabase.co`, IPv6-only y suele no resolver desde redes normales) ni "Transaction pooler" (puerto `6543`, rompe el manejo de sesiones que necesita el checkpointer).
3. El host se ve como `aws-0-<region>.pooler.supabase.com` y el usuario como `postgres.<project-ref>` (no solo `postgres`).
4. Pega esa cadena completa (con tu password) en `DATABASE_URL` dentro de `.env`.

Al arrancar, `agent/graph.py` crea el pool de conexiones y corre `PostgresSaver.setup()` una sola vez (crea sus propias tablas de checkpoint en Postgres si no existen; en corridas siguientes no hace nada). Si `DATABASE_URL` esta mal o no resuelve, el backend falla al arrancar con un error claro apenas intenta importar `routes.chat_routes`.

### Firebase (notificaciones push)

Para que las notificaciones push funcionen en local, hay dos opciones (ver `services/push_notifications.py`):

1. Colocar el archivo de credenciales de servicio de Firebase como `firebase-credentials.json` en la raiz del proyecto (no se sube a git), o
2. Pegar el contenido completo de ese JSON como texto en la variable `FIREBASE_CREDENTIALS_JSON` del `.env` (asi se hace en produccion, por ejemplo en Railway).

Si ninguna de las dos esta configurada, el resto del backend sigue funcionando; simplemente no se enviaran notificaciones push.

## Ejecutar el proyecto

```bash
uvicorn main:app --reload
```

El servidor queda disponible en `http://localhost:8000`. Al arrancar tambien se inicia un scheduler en segundo plano (APScheduler) que revisa cada 5 minutos si hay recordatorios de citas pendientes por enviar.

Documentacion interactiva de la API (Swagger UI) disponible en `http://localhost:8000/docs`.

## Pruebas

Las pruebas unitarias usan `pytest` y viven en `tests/`. Sus dependencias van aparte en `requirements-dev.txt` (no se instalan en la imagen de Docker de produccion):

```bash
pip install -r requirements-dev.txt
```

Comandos:

```bash
# Correr todas las pruebas (~4 segundos)
pytest

# Con reporte de cobertura en la terminal (lineas sin cubrir incluidas).
# Falla si la cobertura total baja de 90% (ver fail_under en .coveragerc)
pytest --cov

# Reporte de cobertura navegable en HTML (se genera en htmlcov/index.html)
pytest --cov --cov-report=html

# Un solo archivo
pytest tests/test_tools.py

# Solo las pruebas cuyo nombre contenga un texto
pytest -k zona

# Detenerse en la primera falla y mostrar el detalle completo
pytest -x -vv
```

**En el pipeline** (`.github/workflows/deploy.yml`), cada push a `main` corre primero `pytest --cov`: si alguna prueba falla o la cobertura baja de 90%, no se construye la imagen ni se despliega.

**Las pruebas no tocan nada real.** El `.env` local apunta a produccion (Supabase, OpenAI, WhatsApp, Wompi), asi que `tests/conftest.py`, antes de importar cualquier codigo de la app:

- ignora el `.env` y usa variables de entorno falsas
- bloquea la red: si a una prueba le falta un mock, falla con `RedBloqueadaEnPruebas` en vez de llegar a produccion
- reemplaza Supabase por una base en memoria (`tests/fakes.py:FakeSupabase`)
- reemplaza el checkpointer de Postgres del agente por uno en memoria
- desactiva el scheduler de recordatorios

Las pruebas del agente ejecutan el grafo real de LangGraph con las tools reales; solo el modelo de IA se reemplaza por respuestas guionadas (`tests/fakes.py:ModeloGuionado`). Ver la seccion "Tests" de `CLAUDE.md` para los fixtures disponibles (`db`, `cliente`, `como`).

## Chat API

Endpoints publicos (sin autenticacion — el enlace es la unica "credencial") aislados por negocio, pensados para que un frontend de cliente final los consuma. El aislamiento entre negocios esta garantizado en dos capas: el `business_id` en la URL, y el `thread_id` interno del checkpointer (`business_id:session_id`), asi que un `session_id` jamas puede continuar la conversacion de otro negocio.

| Metodo y ruta | Que hace |
|---|---|
| `GET /chat/{business_id}/config` | Info publica para que el widget arranque: `{business_id, name, business_type, enabled}`. `enabled` es `false` si el negocio tiene `plan == "none"` o esta bloqueado (`blocked`). 404 si el negocio no existe. |
| `POST /chat/{business_id}/sessions` | Crea una sesion de chat nueva (conversacion vacia) y devuelve `{"session_id": "<uuid>"}`. El frontend la guarda (ej. `localStorage`) y la reutiliza en cada mensaje de esa visita; para empezar de cero, crea otra sesion. |
| `POST /chat/{business_id}/sessions/{session_id}/messages` | Body `{"mensaje": "..."}` → `{"respuesta": "..."}`. Manda el mensaje del cliente al agente y devuelve su respuesta. En este chat general, si el negocio tiene mas de un empleado activo, el propio agente le pregunta al cliente con quien quiere la cita. |
| `GET /chat/{business_id}/sessions/{session_id}/messages` | `{"session_id", "mensajes": [{"role": "user"|"assistant", "content": "..."}]}`. Historial de la sesion, util para recargarlo si el cliente refresca la pagina. |

Cada empleado tiene ademas su propio enlace de chat, con el mismo contrato pero el empleado ya fijo (el agente no pregunta, y solo ofrece los servicios de ese empleado): `GET /chat/{business_id}/{employee_id}/config` (incluye `employee_id`/`employee_name`), `POST /chat/{business_id}/{employee_id}/sessions`, `POST /chat/{business_id}/{employee_id}/sessions/{session_id}/messages`, `GET /chat/{business_id}/{employee_id}/sessions/{session_id}/messages`. 404 si el empleado no existe, no es de ese negocio, o esta inactivo.

Ejemplo de flujo completo con `curl`:

```bash
BUSINESS_ID="<id-de-un-negocio-existente>"

# 1. El frontend chequea si el negocio tiene el chat activo
curl "http://localhost:8000/chat/$BUSINESS_ID/config"

# 2. Crea una sesion nueva
SESSION_ID=$(curl -s -X POST "http://localhost:8000/chat/$BUSINESS_ID/sessions" | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")

# 3. Conversa
curl -X POST "http://localhost:8000/chat/$BUSINESS_ID/sessions/$SESSION_ID/messages" \
  -H "Content-Type: application/json" \
  -d '{"mensaje": "Hola, quiero agendar una cita"}'

# 4. Recarga el historial (ej. al refrescar la pagina)
curl "http://localhost:8000/chat/$BUSINESS_ID/sessions/$SESSION_ID/messages"
```

El agente (`agent/`) identifica al cliente por su numero de WhatsApp/celular, nunca por cedula: lo pide en la conversacion cuando hace falta (agendar, consultar, cancelar o reprogramar una cita) y lo guarda en el estado de la sesion mediante la tool `registrar_telefono_cliente`, asi no hay que repetirlo en cada mensaje. Todas las acciones del agente (consultar servicios/horas, agendar, consultar/cancelar/reprogramar citas, transferir a una persona) pasan por tools — ver `agent/tools.py`.

**Nota:** el bot de IA conversacional ya no responde por WhatsApp. `POST /webhook` (Meta) y `POST /baileys/message` siguen activos pero ya no invocan al agente; WhatsApp queda solo para recordatorios salientes y la confirmacion automatica de citas agendadas en la web.

## WebSocket de tiempo real

`GET /ws/appointments?business_id=<id>` (protocolo `ws://`/`wss://`, no HTTP — por eso no aparece en `/docs`, OpenAPI no documenta WebSockets) empuja al panel un evento cada vez que una cita del negocio se crea, se actualiza o se cancela, sin importar el canal de origen (agente de IA, creacion manual desde el panel, o confirmacion via WhatsApp/webhook de Supabase). Lo consume el frontend Angular para la campana de notificaciones del header (con sonido) y para recargar el calendario de citas automaticamente.

**Autenticacion** (no va en query string para no dejar el JWT en logs de proxies): tras conectar, el cliente debe mandar como primer mensaje un frame de texto JSON `{"type": "auth", "token": "<jwt de Supabase Auth>"}` — el mismo token que ya usa como `Authorization: Bearer` en las llamadas REST. El servidor responde cerrando la conexion si algo falla, con un codigo de cierre WS custom (rango `4000-4999`, reservado para uso de la aplicacion):

| Codigo de cierre | Motivo |
|---|---|
| `4400` | El primer mensaje no es JSON valido |
| `4401` | Token invalido/expirado, o no llego `token` |
| `4403` | El usuario del token no tiene acceso a `business_id` (ni es el dueño ni un empleado activo — misma regla que `verificar_acceso_negocio`) |
| `4408` | No llego ningun mensaje en los primeros 10 segundos tras conectar |

Una vez autenticado, el servidor no espera nada mas del cliente (cualquier mensaje adicional se ignora; solo se sigue escuchando para detectar la desconexion) y empuja un mensaje JSON por evento:

```json
{
  "type": "appointment.created",
  "business_id": "…",
  "appointment": { "...": "mismo shape que cada fila de GET /appointments (incluye services y employees resueltos por join)" }
}
```

`type` es `"appointment.created"`, `"appointment.updated"` (reprogramada) o `"appointment.cancelled"`. Implementacion: `services/realtime.py` (el gestor de conexiones, en memoria por proceso — no hay pub/sub entre instancias, ver limitacion abajo) y `routes/realtime_routes.py` (el endpoint); se dispara desde `agent/tools.py` (`crear_cita`/`cancelar_cita`/`reprogramar_cita`), `routes/manual_appointments.py` y `routes/webhook.py` (`/notify-web-booking` y `/supabase-webhook`).

**Limitacion conocida:** las conexiones se guardan en memoria del proceso de FastAPI. Si el backend corre con mas de un worker/instancia (Railway con >1 replica, por ejemplo), un panel conectado al worker A no se entera de una cita creada por una request que cayo en el worker B. Mientras el backend corra como una sola instancia esto no es un problema; si se escala horizontalmente, esta capa necesitaria moverse a un pub/sub compartido (ej. Postgres `LISTEN/NOTIFY`, o Redis).

## Notificaciones de WhatsApp al cliente (confirmacion de cita)

Ademas del numero propio que cada negocio puede conectar (`businesses.whatsapp_phone_number_id`, usado para recordatorios y respondiendo como ese negocio), GoAgenda tiene su **propio numero de WhatsApp Business** (+57 301 7682731), compartido por todos los negocios de la plataforma, dedicado exclusivamente a notificar directamente al **cliente final** cuando su cita queda confirmada. Se configura con `GOAGENDA_WHATSAPP_TOKEN` + `GOAGENDA_WHATSAPP_PHONE_NUMBER_ID` (ver `.env.example`); `GOAGENDA_WHATSAPP_BUSINESS_ACCOUNT_ID` no lo usa el envio de mensajes, se guarda solo como referencia del WABA en Meta Business Manager (util para gestionar templates, calidad del numero, etc).

Implementacion en `services/whatsapp.py`:
- `normalizar_numero_whatsapp(numero)` — valida que sea un celular colombiano (10 digitos, empieza en 3, con o sin indicativo 57) y lo devuelve en formato internacional sin `+` (lo que espera la Cloud API como `to`), o `None` si no es valido.
- `enviar_confirmacion_cita_cliente(client_phone, nombre_cliente, nombre_negocio, nombre_servicio, fecha_hora_texto)` — arma y manda el mensaje de confirmacion desde el numero de GoAgenda.

Se dispara (best-effort, envuelto en `try/except` — nunca bloquea la creacion de la cita si falla) desde los dos lugares donde una cita nace ya `confirmed`: `agent/tools.py:crear_cita` (agenda por el chat de IA) y `routes/manual_appointments.py:crear_cita_manual` (creacion desde el panel; ahi el numero no es obligatoriamente un WhatsApp, asi que si no normaliza a uno valido simplemente no se envia nada).

**El chat de IA ahora exige un numero de WhatsApp, no cualquier telefono**: `agent/tools.py:registrar_telefono_cliente` usa `normalizar_numero_whatsapp` para validar lo que da el cliente (rechaza fijos y numeros mal formados, pidiendo que lo reescriba) antes de guardarlo como `client_phone` — el mismo numero que despues recibe la confirmacion. El prompt del agente (`agent/prompts.py`) y los mensajes de error de las demas tools se actualizaron para pedir explicitamente "tu numero de WhatsApp", ya no "telefono o celular".

**Ventana de 24 horas de Meta (por que se manda un template, no texto libre):** la Cloud API de WhatsApp solo permite mensajes de texto libre dentro de las 24h desde el ultimo mensaje que el cliente le escribio a ese numero. Como los clientes agendan por el chat web y nunca le han escrito directamente al numero de GoAgenda, un mensaje de texto libre siempre lo rechaza Meta (se confirmó en produccion: status 200 al enviar, pero el webhook de status reporta `"status": "failed"` con `"code": 131047` — "Re-engagement message" — segundos despues). Por eso `enviar_confirmacion_cita_cliente` manda un **message template pre-aprobado** (`type: "template"`, no `"text"`), que si puede iniciar conversacion con un numero nuevo.

El template `confirmacion_cita_goagenda` (categoria `UTILITY`, idioma `es_CO`) ya esta creado en el WABA de GoAgenda via la Graph API (`POST /{waba_id}/message_templates`) con este texto:

```
Hola {{1}}, tu cita en {{2}} quedó confirmada.
Servicio: {{3}}
Fecha y hora: {{4}}
¡Te esperamos!
```

Variables en orden: `nombre_cliente`, `nombre_negocio`, `nombre_servicio`, `fecha_hora_texto` (ver `enviar_confirmacion_cita_cliente` en `services/whatsapp.py`). Meta revisa cada template antes de poder usarlo (minutos a horas) — mientras el status en Meta Business Manager > WhatsApp Manager > Message Templates diga `PENDING`, los envios fallaran con un error tipo "template not found"/"not approved"; una vez quede `APPROVED` empiezan a entregarse. Si se necesita cambiar la copia del mensaje, hay que crear (o editar) el template en Meta con el texto nuevo *antes* de tocar el codigo, porque el texto enviado tiene que calzar exactamente con el aprobado. El nombre/idioma del template son configurables por `GOAGENDA_WHATSAPP_CONFIRMATION_TEMPLATE`/`GOAGENDA_WHATSAPP_CONFIRMATION_TEMPLATE_LANG` si se crea uno nuevo con otro nombre.

Para probar el envio real sin tener que agendar una cita, usar `POST /test-whatsapp-confirmation?numero_whatsapp=<numero>` (protegido con header `x-api-key`, ver `main.py`) contra un numero real, una vez el template este `APPROVED`.

**Plantillas v2 (confirmación, reprogramación, recordatorio y cancelación con fecha, hora, profesional y dirección):** textos, variables, cómo crearlas en Meta y cómo activarlas sin riesgo en [`docs/PLANTILLAS_WHATSAPP.md`](docs/PLANTILLAS_WHATSAPP.md). Quedan apagadas (`WHATSAPP_PLANTILLAS_VERSION=v1`) hasta que Meta las apruebe.

## Recordatorios de cita por WhatsApp (worker en segundo plano)

`services/reminder_service.py:revisar_y_enviar_recordatorios` corre cada 5 minutos via `APScheduler` (registrado en `main.py`, junto al `BackgroundScheduler`). Por cada negocio, busca citas `confirmed` con `reminder_sent = false` cuyo `scheduled_at` cae dentro de la ventana `reminder_hours_before` (configurable por negocio, ver `PUT /business-settings/reminder`), y le manda el recordatorio al `client_phone` de la cita, desde el numero propio del negocio (`businesses.whatsapp_phone_number_id`) via Meta, o por Baileys si el negocio no tiene numero de Meta conectado.

**Misma ventana de 24 horas que la confirmacion, pero por negocio-cliente, no por GoAgenda-cliente.** A diferencia de la confirmacion (que siempre sale del numero compartido de GoAgenda, donde el cliente nunca ha escrito), el recordatorio sale del numero propio de cada negocio — y ahi si es posible que el cliente ya le haya escrito antes (por ejemplo, si coordino la cita por WhatsApp directamente). Por eso se necesita saber, por cada `(business_id, client_phone)`, cuando fue el ultimo mensaje que el cliente le mando a ese negocio:

- Tabla `whatsapp_client_contacts` (migracion `whatsapp_client_contacts.sql`, correrla en el SQL Editor de Supabase igual que las demas migraciones del repo): `business_id`, `client_phone`, `last_inbound_at`.
- `routes/webhook.py:receive_message` llama a `services/db.py:registrar_mensaje_entrante_whatsapp` en cada mensaje entrante (antes de filtrar por si trae texto), asi que cualquier mensaje del cliente renueva la ventana.
- `services/db.py:cliente_dentro_de_ventana_24h` revisa si pasaron menos de 24h desde ese `last_inbound_at`. Si nunca hay fila (primera vez que se le escribe) o ya pasaron mas de 24h, devuelve `False`.
- `services/reminder_service.py` usa ese resultado para decidir: dentro de la ventana manda texto libre (`send_whatsapp_message`, como siempre); fuera de la ventana manda `services/whatsapp.py:enviar_recordatorio_cita_template`, un message template aprobado (`WHATSAPP_REMINDER_TEMPLATE`/`WHATSAPP_REMINDER_TEMPLATE_LANG`, ver `.env.example`), igual que hace la confirmacion de cita.
- La rama de Baileys (negocios sin `whatsapp_phone_number_id`) no cambia — Baileys no tiene concepto de message template, siempre manda texto libre.

**El template `recordatorio_cita_goagenda` todavia NO esta creado en Meta Business Manager** — hay que crearlo y esperar a que quede `APPROVED` antes de que el envio fuera de la ventana de 24h funcione (mientras tanto fallara igual que un template `PENDING`, ver la seccion de confirmacion arriba). Usar el mismo texto de la confirmacion como referencia, con las 4 variables en el mismo orden: `nombre_cliente`, `nombre_negocio`, `nombre_servicio`, `fecha_hora_texto`.

Para probar el envio de la plantilla sin esperar al scheduler ni a una cita real, usar `POST /test-whatsapp-reminder-template?business_id=<id>&numero_whatsapp=<numero>` (protegido con `x-api-key`), o `POST /test-reminders` para forzar una corrida completa de `revisar_y_enviar_recordatorios`.

## Retención de conversaciones del chat

El historial de cada conversación del chat público (donde quedan el nombre y el WhatsApp del cliente) vive en los checkpoints de LangGraph. `services/retencion_conversaciones.py` elimina cada conversación **a los `CHAT_RETENCION_DIAS` (3) días de su última actividad**, una por una: la de un cliente se borra sin afectar la de otro.

- **Qué se borra:** solo el historial (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`). **No** se tocan citas, reservas, pagos ni `chat_sessions` (contadores y fechas, sin contenido; alimenta las métricas de conversión).
- **Cuándo corre:** una vez al día (`CHAT_LIMPIEZA_HORA_UTC`, 08:00 UTC = 03:00 Colombia), con cron en el scheduler. El plazo y la frecuencia son independientes. `CHAT_RETENCION_DIAS=0` la desactiva.
- **Efecto para el cliente:** si vuelve después del plazo con el mismo navegador, su historial aparece vacío y el asistente empieza de cero (vuelve a pedirle el WhatsApp).
- **Ensayo antes de borrar:** `POST /admin/chat-retention/run` (super admin) cuenta cuántas se eliminarían sin borrar nada (`dry_run=true` por defecto); con `?dry_run=false` ejecuta la limpieza real.
- **Primera corrida:** elimina todo lo que ya tenga más de 3 días de inactividad, incluido el historial anterior a esta función. Es irreversible; conviene hacer el ensayo antes.

## Variables de entorno

Ver `.env.example` para la lista completa con descripciones. Resumen:

| Variable | Para que sirve |
|---|---|
| `SUPABASE_URL`, `SUPABASE_KEY` | Conexion a Supabase (REST API) |
| `SUPABASE_SERVICE_ROLE_KEY` | Clave server-side recomendada para operaciones backend que deben bypassear RLS |
| `DATABASE_URL` | Conexion Postgres directa (Session pooler) para el checkpointer del agente de chat — ver seccion arriba |
| `OPENAI_API_KEY` | Motor de IA del agente de citas |
| `CHAT_MODEL_NAME` | Modelo de OpenAI a usar (opcional, default `gpt-4.1-mini`) |
| `WHATSAPP_TOKEN` | Token del System User de Meta para enviar mensajes salientes por WhatsApp Cloud API (numero propio de cada negocio) |
| `VERIFY_TOKEN` | Token propio para validar el webhook de Meta |
| `GOAGENDA_WHATSAPP_TOKEN`, `GOAGENDA_WHATSAPP_PHONE_NUMBER_ID` | Numero de WhatsApp Business propio de GoAgenda (compartido, +57 301 7682731) para notificar directamente al cliente final (confirmacion de cita) — ver seccion arriba |
| `GOAGENDA_WHATSAPP_BUSINESS_ACCOUNT_ID` | Solo referencia (WABA en Meta Business Manager); no lo usa el codigo de envio |
| `WHATSAPP_REMINDER_TEMPLATE`, `WHATSAPP_REMINDER_TEMPLATE_LANG` | Message template aprobado en Meta para el recordatorio de cita fuera de la ventana de 24h — ver seccion "Recordatorios de cita por WhatsApp" |
| `BAILEYS_SERVICE_URL`, `BAILEYS_INTERNAL_API_KEY` | URL y api key compartida con el microservicio de Baileys (WhatsApp no oficial), usado para recordatorios y vinculacion |
| `CORS_ALLOWED_ORIGINS` | Origenes permitidos del panel admin (lista separada por comas) |
| `FIREBASE_CREDENTIALS_JSON` | Credenciales de Firebase para notificaciones push (alternativa a `firebase-credentials.json`) |

## Estructura del proyecto

```
main.py               # arranque de FastAPI, registro de routers, scheduler de recordatorios
core/                  # configuracion compartida (settings del agente de chat)
agent/                 # agente conversacional de LangGraph: estado, tools, prompt, grafo
routes/                # un router por recurso (chat, webhook, negocios, servicios, horarios, realtime, etc)
services/               # logica de negocio: Supabase, WhatsApp, Baileys, push, realtime (WS), uso de IA, scheduling
tests/                  # pruebas unitarias (pytest); conftest.py aisla todo de los servicios reales
```

Para una guia mas detallada de la arquitectura (flujo del agente, aislamiento por negocio, modelo de auth, etc), ver `CLAUDE.md`.
