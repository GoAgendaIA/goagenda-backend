import os
import requests
from dotenv import load_dotenv

load_dotenv()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")  # mismo token (System User) sirve para todos los numeros de esta app de Meta

# Numero de WhatsApp Business propio de GoAgenda (no de un negocio individual),
# usado para notificar directamente a los CLIENTES finales (ej. confirmacion
# de cita) sin depender de que cada negocio haya conectado su propio numero.
# Token separado porque puede vivir en una app/WABA de Meta distinta a la de
# los numeros por-negocio.
GOAGENDA_WHATSAPP_TOKEN = os.getenv("GOAGENDA_WHATSAPP_TOKEN")
GOAGENDA_WHATSAPP_PHONE_NUMBER_ID = os.getenv("GOAGENDA_WHATSAPP_PHONE_NUMBER_ID")

# Message template aprobado en Meta Business Manager para la confirmacion de
# cita (categoria UTILITY). Necesario porque el cliente agenda por el chat
# web y nunca le ha escrito a este numero: un mensaje de texto libre SIEMPRE
# lo rechaza Meta fuera de la ventana de 24h (error 131047, "re-engagement
# message"). El texto exacto (con las 4 variables en este orden: nombre del
# cliente, nombre del negocio, servicio, fecha/hora) esta documentado en el
# README, seccion WhatsApp - si se edita la copia hay que volver a crear/
# aprobar el template en Meta con el mismo texto antes de cambiarlo aqui.
GOAGENDA_WHATSAPP_CONFIRMATION_TEMPLATE = os.getenv("GOAGENDA_WHATSAPP_CONFIRMATION_TEMPLATE", "confirmacion_cita_goagenda")
GOAGENDA_WHATSAPP_CONFIRMATION_TEMPLATE_LANG = os.getenv("GOAGENDA_WHATSAPP_CONFIRMATION_TEMPLATE_LANG", "es_CO")

# Message template aprobado en Meta Business Manager para el recordatorio de
# cita (categoria UTILITY). Se usa en vez de texto libre cuando el cliente
# nunca le ha escrito al numero propio del negocio, o ya paso mas de 24h
# desde su ultimo mensaje (services/db.py:cliente_dentro_de_ventana_24h) -
# un mensaje de texto libre en ese caso lo rechaza Meta (error 131047).
# El texto exacto (con las 4 variables en este orden: nombre del cliente,
# nombre del negocio, servicio, fecha/hora) debe crearse y aprobarse en
# Meta con el mismo nombre configurado aqui antes de que esto funcione.
WHATSAPP_REMINDER_TEMPLATE = os.getenv("WHATSAPP_REMINDER_TEMPLATE", "recordatorio_cita_goagenda")
WHATSAPP_REMINDER_TEMPLATE_LANG = os.getenv("WHATSAPP_REMINDER_TEMPLATE_LANG", "es_CO")


# --- Plantillas v2 (mejor experiencia: fecha y hora separadas, profesional, direccion y enlaces) -----
# Mientras Meta no las apruebe NO se pueden usar: WHATSAPP_PLANTILLAS_VERSION queda en "v1" (las
# plantillas de arriba, intactas) y la reprogramacion/cancelacion no se envian, porque no existe
# una v1 de ellas. Se pasa a "v2" cuando las cuatro figuren APPROVED en Meta Business Manager.
# Los textos exactos y las variables (posicionales, en este orden) estan en
# docs/PLANTILLAS_WHATSAPP.md.
WHATSAPP_PLANTILLAS_VERSION = os.getenv("WHATSAPP_PLANTILLAS_VERSION", "v1").strip().lower()
WHATSAPP_PLANTILLAS_V2_LANG = os.getenv("WHATSAPP_PLANTILLAS_V2_LANG", "es_CO")

PLANTILLAS_V2 = {
    # tipo: (variable de entorno del nombre, nombre por defecto, variables en orden)
    "confirmacion": ("WHATSAPP_TEMPLATE_CONFIRMACION_V2", "confirmacion_cita_v2_goagenda",
                     ["nombre_cliente", "nombre_negocio", "fecha", "hora", "servicio", "profesional", "direccion"]),
    "reprogramacion": ("WHATSAPP_TEMPLATE_REPROGRAMACION_V2", "reprogramacion_cita_v2_goagenda",
                       ["nombre_cliente", "nombre_negocio", "nueva_fecha", "nueva_hora"]),
    "recordatorio": ("WHATSAPP_TEMPLATE_RECORDATORIO_V2", "recordatorio_cita_v2_goagenda",
                     ["nombre_cliente", "nombre_negocio", "fecha", "hora", "servicio", "direccion", "enlace_reprogramacion"]),
    "cancelacion": ("WHATSAPP_TEMPLATE_CANCELACION_V2", "cancelacion_cita_v2_goagenda",
                    ["nombre_cliente", "nombre_negocio", "fecha", "hora", "servicio", "enlace_agendamiento"]),
}


def plantillas_v2_activas() -> bool:
    return WHATSAPP_PLANTILLAS_VERSION == "v2"


def _nombre_plantilla_v2(tipo: str) -> str:
    variable_entorno, por_defecto, _ = PLANTILLAS_V2[tipo]
    return os.getenv(variable_entorno, por_defecto)


def _enviar_plantilla_v2(tipo: str, to: str, valores: dict):
    """Manda una plantilla v2 desde el numero de GoAgenda. `valores` trae cada variable por nombre."""
    if not GOAGENDA_WHATSAPP_TOKEN or not GOAGENDA_WHATSAPP_PHONE_NUMBER_ID:
        print(f"Faltan GOAGENDA_WHATSAPP_TOKEN/GOAGENDA_WHATSAPP_PHONE_NUMBER_ID, no se envia la plantilla {tipo}.")
        return {"error": "credenciales de WhatsApp de GoAgenda no configuradas"}

    variables = PLANTILLAS_V2[tipo][2]
    parametros = [str(valores[nombre]).strip() or "-" for nombre in variables]
    return _enviar_mensaje_template(
        to=to,
        phone_number_id=GOAGENDA_WHATSAPP_PHONE_NUMBER_ID,
        token=GOAGENDA_WHATSAPP_TOKEN,
        template_name=_nombre_plantilla_v2(tipo),
        language_code=WHATSAPP_PLANTILLAS_V2_LANG,
        parametros_body=parametros,
    )


def construir_mensaje_contacto_humano(business: dict | None) -> str:
    """
    Arma el mensaje que el agente de chat le comparte al cliente cuando
    necesita "rendirse" y pasarlo a un humano: no entendio su solicitud
    pese a intentar aclarar, o hubo un fallo tecnico (ver
    agent/tools.py:escalar_por_confusion y agent/graph.py). Si el telefono
    guardado en businesses.phone_number normaliza a un WhatsApp colombiano
    valido, arma un enlace wa.me clickeable; si no hay telefono o no
    normaliza, cae a un mensaje generico sin numero.
    """
    numero = business.get("phone_number") if business else None
    numero_normalizado = normalizar_numero_whatsapp(numero) if numero else None

    if numero_normalizado:
        return f"📱 Escríbele directo al negocio por WhatsApp: https://wa.me/{numero_normalizado}"
    return "📱 Contacta directamente al negocio, en un momento te ayudan."


def normalizar_numero_whatsapp(numero: str) -> str | None:
    """
    Valida y normaliza un numero de WhatsApp a formato internacional sin
    "+" (el que espera la Cloud API de Meta como "to" al enviar mensajes),
    asumiendo Colombia cuando el cliente lo da sin indicativo. Devuelve
    None si no parece un celular colombiano valido: 10 digitos empezando
    en 3 (con o sin el indicativo 57 adelante). No acepta telefonos fijos
    (no reciben WhatsApp). La usan tanto el agente de chat
    (agent/tools.py:registrar_telefono_cliente) como la creacion manual de
    citas desde el panel (routes/manual_appointments.py).
    """
    limpio = "".join(c for c in numero if c.isdigit())

    if limpio.startswith("57") and len(limpio) == 12:
        limpio = limpio[2:]

    if len(limpio) == 10 and limpio[0] == "3":
        return f"57{limpio}"

    return None


def _enviar_mensaje_texto(to: str, text: str, phone_number_id: str, token: str):
    """
    POST generico a la Cloud API de Meta (`/{phone_number_id}/messages`) con
    un mensaje de texto libre. Nota: Meta solo permite texto libre dentro de
    la ventana de 24h desde el ultimo mensaje del cliente a ese numero; para
    contactar primero a un numero que nunca escribio (como los clientes que
    reservan por el chat web y jamas le escribieron a este numero) hace
    falta un message template pre-aprobado. Ver README, seccion WhatsApp.
    """
    graph_api_url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }

    try:
        response = requests.post(graph_api_url, headers=headers, json=payload, timeout=15)
        print(f"Meta API status: {response.status_code}")
        print(f"Meta API response: {response.text}")

        if response.status_code != 200:
            return {"error": response.json()}

        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error de conexion con la API de Meta: {e}")
        return {"error": str(e)}


def _enviar_mensaje_template(to: str, phone_number_id: str, token: str, template_name: str, language_code: str, parametros_body: list[str]):
    """
    POST a la Cloud API de Meta con un mensaje de tipo "template". A
    diferencia del texto libre, un template aprobado por Meta SI puede
    iniciar una conversacion con un numero que nunca le ha escrito a este
    numero de WhatsApp - lo que aplica siempre a los clientes que agendan
    por el chat web.
    """
    graph_api_url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": valor} for valor in parametros_body],
                }
            ],
        },
    }

    try:
        response = requests.post(graph_api_url, headers=headers, json=payload, timeout=15)
        print(f"Meta API (template) status: {response.status_code}")
        print(f"Meta API (template) response: {response.text}")

        if response.status_code != 200:
            return {"error": response.json()}

        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error de conexion con la API de Meta: {e}")
        return {"error": str(e)}


def send_whatsapp_message(to: str, text: str, business_phone_number_id: str):
    """
    Envia un mensaje de WhatsApp usando la API oficial de Meta (Cloud API),
    desde el numero especifico del negocio (business_phone_number_id).
    Esto permite que cada negocio responda desde su propio numero de WhatsApp,
    aunque todos compartan el mismo token de System User.
    """
    return _enviar_mensaje_texto(to, text, business_phone_number_id, WHATSAPP_TOKEN)


def enviar_confirmacion_cita_cliente(
    client_phone: str,
    nombre_cliente: str,
    nombre_negocio: str,
    nombre_servicio: str,
    fecha_hora_texto: str,
    *,
    datos_plantilla: dict | None = None,
):
    """
    Le confirma al cliente, por WhatsApp, que su cita quedo agendada. Se
    manda como message template (GOAGENDA_WHATSAPP_CONFIRMATION_TEMPLATE),
    no como texto libre: el cliente agenda por el chat web y nunca le ha
    escrito a este numero, asi que un mensaje de texto libre lo rechaza
    Meta siempre por estar fuera de la ventana de 24h.

    Con la plantilla v2 activa y `datos_plantilla` (ver
    services/notificaciones_cliente.py:datos_para_plantilla) se manda la
    version con fecha, hora, profesional y direccion.
    """
    if plantillas_v2_activas() and datos_plantilla:
        return _enviar_plantilla_v2("confirmacion", client_phone, {
            "nombre_cliente": nombre_cliente, "nombre_negocio": nombre_negocio, "servicio": nombre_servicio,
            "fecha": datos_plantilla["fecha"], "hora": datos_plantilla["hora"],
            "profesional": datos_plantilla["profesional"], "direccion": datos_plantilla["direccion"],
        })

    if not GOAGENDA_WHATSAPP_TOKEN or not GOAGENDA_WHATSAPP_PHONE_NUMBER_ID:
        print("Faltan GOAGENDA_WHATSAPP_TOKEN/GOAGENDA_WHATSAPP_PHONE_NUMBER_ID, no se envia la confirmacion al cliente.")
        return {"error": "credenciales de WhatsApp de GoAgenda no configuradas"}

    return _enviar_mensaje_template(
        to=client_phone,
        phone_number_id=GOAGENDA_WHATSAPP_PHONE_NUMBER_ID,
        token=GOAGENDA_WHATSAPP_TOKEN,
        template_name=GOAGENDA_WHATSAPP_CONFIRMATION_TEMPLATE,
        language_code=GOAGENDA_WHATSAPP_CONFIRMATION_TEMPLATE_LANG,
        parametros_body=[nombre_cliente, nombre_negocio, nombre_servicio, fecha_hora_texto],
    )


def enviar_recordatorio_cita_template(
    to: str,
    nombre_cliente: str,
    nombre_negocio: str,
    nombre_servicio: str,
    fecha_hora_texto: str,
    *,
    datos_plantilla: dict | None = None,
):
    """
    Envia el recordatorio de cita como message template (WHATSAPP_REMINDER_TEMPLATE)
    desde el numero propio de GoAgenda, igual que la confirmacion, para todos
    los negocios (multitenant): el nombre del negocio va como variable de la
    plantilla, asi ningun negocio necesita tener su propio numero de Meta.
    Siempre template y no texto libre, porque el cliente agenda por el chat
    web y Meta rechaza texto libre fuera de la ventana de 24h.
    """
    if plantillas_v2_activas() and datos_plantilla:
        return _enviar_plantilla_v2("recordatorio", to, {
            "nombre_cliente": nombre_cliente, "nombre_negocio": nombre_negocio, "servicio": nombre_servicio,
            "fecha": datos_plantilla["fecha"], "hora": datos_plantilla["hora"], "direccion": datos_plantilla["direccion"],
            "enlace_reprogramacion": datos_plantilla["enlace_reprogramacion"],
        })

    if not GOAGENDA_WHATSAPP_TOKEN or not GOAGENDA_WHATSAPP_PHONE_NUMBER_ID:
        print("Faltan GOAGENDA_WHATSAPP_TOKEN/GOAGENDA_WHATSAPP_PHONE_NUMBER_ID, no se envia el recordatorio.")
        return {"error": "credenciales de WhatsApp de GoAgenda no configuradas"}

    return _enviar_mensaje_template(
        to=to,
        phone_number_id=GOAGENDA_WHATSAPP_PHONE_NUMBER_ID,
        token=GOAGENDA_WHATSAPP_TOKEN,
        template_name=WHATSAPP_REMINDER_TEMPLATE,
        language_code=WHATSAPP_REMINDER_TEMPLATE_LANG,
        parametros_body=[nombre_cliente, nombre_negocio, nombre_servicio, fecha_hora_texto],
    )


def enviar_reprogramacion_cita_cliente(client_phone: str, nombre_cliente: str, nombre_negocio: str, datos_plantilla: dict):
    """
    Avisa al cliente que su cita fue reprogramada (fecha y hora NUEVAS en
    `datos_plantilla`). Solo existe como plantilla v2: con la version v1 no se
    envia nada (no hay una plantilla anterior de reprogramacion) y se devuelve
    {"omitido": ...} para que quien llama no lo cuente como falla.
    """
    if not plantillas_v2_activas():
        return {"omitido": "plantillas v2 de WhatsApp no activas"}
    return _enviar_plantilla_v2("reprogramacion", client_phone, {
        "nombre_cliente": nombre_cliente, "nombre_negocio": nombre_negocio,
        "nueva_fecha": datos_plantilla["fecha"], "nueva_hora": datos_plantilla["hora"],
    })


def enviar_cancelacion_cita_cliente(
    client_phone: str, nombre_cliente: str, nombre_negocio: str, nombre_servicio: str, datos_plantilla: dict
):
    """
    Avisa al cliente que su cita fue cancelada (fecha y hora de la cita cancelada
    en `datos_plantilla`), con el enlace para reservar de nuevo. Igual que la
    reprogramacion, solo existe como plantilla v2.
    """
    if not plantillas_v2_activas():
        return {"omitido": "plantillas v2 de WhatsApp no activas"}
    return _enviar_plantilla_v2("cancelacion", client_phone, {
        "nombre_cliente": nombre_cliente, "nombre_negocio": nombre_negocio, "servicio": nombre_servicio,
        "fecha": datos_plantilla["fecha"], "hora": datos_plantilla["hora"],
        "enlace_agendamiento": datos_plantilla["enlace_agendamiento"],
    })
