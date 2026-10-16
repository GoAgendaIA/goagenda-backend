# Plantillas de WhatsApp para el cliente (v2)

Mejora de la experiencia del cliente final: los cuatro mensajes que recibe por WhatsApp
(confirmación, reprogramación, recordatorio y cancelación) con fecha y hora separadas,
profesional, dirección del negocio y enlaces.

Las envía el número propio de GoAgenda (`GOAGENDA_WHATSAPP_PHONE_NUMBER_ID`). Como el
cliente nunca le ha escrito a ese número, **Meta solo permite plantillas aprobadas** (un
texto libre se rechaza fuera de la ventana de 24 h, error `131047`). Por eso este cambio
es en dos pasos: primero se crean y aprueban las plantillas en Meta, después se activa en
el código.

## Cómo se activa (y por qué es seguro)

| `WHATSAPP_PLANTILLAS_VERSION` | Qué pasa |
|---|---|
| `v1` (por defecto) | Todo igual que hoy: confirmación y recordatorio con las plantillas actuales (`confirmacion_cita_goagenda`, `recordatorio_cita_goagenda`). Reprogramación y cancelación **no se envían** (no existía plantilla). |
| `v2` | Los cuatro mensajes con las plantillas de este documento. |

Pasar a `v2` solo cuando las **cuatro** figuren `APPROVED` en *Meta Business Manager →
WhatsApp Manager → Message Templates*. Si se activa antes, los envíos fallan con "template
not found" (la cita y el resto del flujo no se ven afectados: el envío es *best-effort*).

Para revertir basta volver a `v1`; no hace falta desplegar código.

Nombres configurables por entorno si alguna se crea con otro nombre:
`WHATSAPP_TEMPLATE_CONFIRMACION_V2`, `WHATSAPP_TEMPLATE_REPROGRAMACION_V2`,
`WHATSAPP_TEMPLATE_RECORDATORIO_V2`, `WHATSAPP_TEMPLATE_CANCELACION_V2`, e idioma con
`WHATSAPP_PLANTILLAS_V2_LANG` (por defecto `es_CO`).

## Las cuatro plantillas

Categoría **UTILITY**, idioma **es_CO**. Las variables son **posicionales** (`{{1}}`,
`{{2}}`…) y el código las manda en este orden. El texto enviado a Meta debe ser
exactamente el de cada bloque.

### `confirmacion_cita_v2_goagenda`

```
¡Hola, {{1}}! 👋

Tu cita en {{2}} ha sido confirmada exitosamente. ✅

📅 Fecha: {{3}}
🕐 Hora: {{4}}
📌 Servicio: {{5}}
👤 Profesional: {{6}}
📍 Dirección: {{7}}

¡Te esperamos! Gracias por elegirnos. 😊
```

Variables: 1 `nombre_cliente`, 2 `nombre_negocio`, 3 `fecha`, 4 `hora`, 5 `servicio`, 6 `profesional`, 7 `direccion`.

### `reprogramacion_cita_v2_goagenda`

```
¡Hola, {{1}}! 👋

Tu cita en {{2}} ha sido reprogramada exitosamente. 🔄

📅 Nueva fecha: {{3}}
🕐 Nueva hora: {{4}}

¡Gracias por tu comprensión! 😊
```

Variables: 1 `nombre_cliente`, 2 `nombre_negocio`, 3 `nueva_fecha`, 4 `nueva_hora`.

### `recordatorio_cita_v2_goagenda`

```
¡Hola, {{1}}! 👋

Te recordamos que tienes una cita próxima en {{2}}. ⏰

📅 Fecha: {{3}}
🕐 Hora: {{4}}
📌 Servicio: {{5}}
📍 Dirección: {{6}}

¿Necesitas cambiar tu cita? Puedes reprogramarla aquí:

🔗 {{7}}

¡Te esperamos! 😊
```

Variables: 1 `nombre_cliente`, 2 `nombre_negocio`, 3 `fecha`, 4 `hora`, 5 `servicio`, 6 `direccion`, 7 `enlace_reprogramacion`.

### `cancelacion_cita_v2_goagenda`

```
¡Hola, {{1}}! 👋

Te informamos que tu cita en {{2}} ha sido cancelada. ❌

📅 Fecha: {{3}}
🕐 Hora: {{4}}
📌 Servicio: {{5}}

Si deseas reservar una nueva cita, puedes hacerlo aquí:

🔗 {{6}}

¡Esperamos atenderte pronto! 😊
```

Variables: 1 `nombre_cliente`, 2 `nombre_negocio`, 3 `fecha`, 4 `hora`, 5 `servicio`, 6 `enlace_agendamiento`.

## Qué valores lleva cada variable

Los arma `services/notificaciones_cliente.py:datos_para_plantilla`:

| Variable | Valor | Si falta el dato |
|---|---|---|
| `fecha` | `miércoles 14 de octubre` | — |
| `hora` | `3:00 pm` (siempre 12 h) | — |
| `profesional` | nombre del empleado de la cita | `Nuestro equipo` |
| `direccion` | dirección del negocio + municipio/barrio | `Consulta la dirección con el negocio` (negocios anteriores al campo) |
| `enlace_reprogramacion` / `enlace_agendamiento` | chat público del negocio: `GOAGENDA_FRONTEND_URL/chat/<business_id>` | — |

Meta rechaza parámetros vacíos, por eso ningún valor sale en blanco.

**Reprogramación:** `fecha`/`hora` son las **nuevas**. **Cancelación:** son las de la cita
**cancelada**.

## Cómo crearlas en Meta

Desde WhatsApp Manager → *Message Templates → Create template*, o por la Graph API
(`POST /{waba_id}/message_templates`), una por plantilla. Ejemplo para la cancelación:

```json
{
  "name": "cancelacion_cita_v2_goagenda",
  "language": "es_CO",
  "category": "UTILITY",
  "components": [
    {
      "type": "BODY",
      "text": "¡Hola, {{1}}! 👋\n\nTe informamos que tu cita en {{2}} ha sido cancelada. ❌\n\n📅 Fecha: {{3}}\n🕐 Hora: {{4}}\n📌 Servicio: {{5}}\n\nSi deseas reservar una nueva cita, puedes hacerlo aquí:\n\n🔗 {{6}}\n\n¡Esperamos atenderte pronto! 😊",
      "example": {
        "body_text": [["Ana", "Barbería Uno", "miércoles 14 de octubre", "3:00 pm", "Corte de cabello", "https://app.goagenda.xyz/chat/abc"]]
      }
    }
  ]
}
```

Reglas de Meta a tener en cuenta (ya cumplidas en estos textos): una variable no puede ir
al inicio ni al final del mensaje, dos variables no pueden ir pegadas, y el texto debe
dar contexto suficiente alrededor de cada una. Meta suele revisar en minutos u horas.

## Dónde se envía cada mensaje

| Mensaje | Se dispara desde |
|---|---|
| Confirmación | `services/appointment_confirmation.py` (cita del chat, y cita con abono al confirmarse el pago) y `routes/manual_appointments.py` (cita manual del panel con WhatsApp válido) |
| Recordatorio | `services/reminder_service.py` (cada 5 min, según `reminder_hours_before`) |
| Reprogramación | `agent/tools.py:reprogramar_cita` |
| Cancelación | `agent/tools.py:cancelar_cita` |

Todos son *best-effort*: si el envío falla (Meta caído, plantilla sin aprobar) se registra
en el log y la acción del cliente se completa igual.

## Limitaciones

- La **reprogramación y la cancelación solo ocurren hoy por el chat de IA**: el panel no
  tiene todavía una acción para cancelar o mover una cita, así que no hay desde dónde
  disparar el aviso. Cuando exista, debe llamar a `enviar_cancelacion_cita_cliente` /
  `enviar_reprogramacion_cita_cliente`.
- Las citas **cancelas automáticamente por pago vencido** (`cancelled_by=system`) no avisan
  al cliente por este medio.
- Los negocios que ya existían no tienen dirección hasta que la llenen (Ajustes → General);
  mientras tanto el mensaje dice «Consulta la dirección con el negocio».
