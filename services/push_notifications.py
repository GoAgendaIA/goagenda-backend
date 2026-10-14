import firebase_admin
from firebase_admin import credentials, messaging
import os
import json

_firebase_app = None

# TODO: pega aqui la URL publica real de tu logo subido a Supabase Storage
LOGO_URL = "https://ghwkiuwcovrmpztamjgz.supabase.co/storage/v1/object/public/assets/icono_goagenda.png"


def _inicializar_firebase():
    """
    Inicializa Firebase Admin SDK una sola vez (singleton).
    Soporta 2 formas de dar las credenciales:
    1. Archivo local firebase-credentials.json (desarrollo en tu PC)
    2. Variable de entorno FIREBASE_CREDENTIALS_JSON con el contenido
       completo del JSON pegado como texto (produccion en Railway, donde
       no es practico subir un archivo)
    """
    global _firebase_app
    if _firebase_app is None:
        credenciales_env = os.getenv("FIREBASE_CREDENTIALS_JSON")
        if credenciales_env:
            cred_dict = json.loads(credenciales_env)
            cred = credentials.Certificate(cred_dict)
        else:
            ruta_credenciales = os.path.join(os.path.dirname(__file__), "..", "firebase-credentials.json")
            cred = credentials.Certificate(ruta_credenciales)
        _firebase_app = firebase_admin.initialize_app(cred)
    return _firebase_app


def _enviar_push(fcm_token: str, titulo: str, cuerpo: str, data: dict[str, str] | None = None):
    """
    Funcion generica que envia una notificacion push al dispositivo del negocio.
    Si fcm_token es None o vacio, no hace nada (el negocio no ha registrado su dispositivo aun).
    `data` es un payload adicional de pares texto-texto (ej. business_id/session_id
    para armar un deep link) que la app puede leer al tocar la notificacion;
    FCM lo soporta aparte de title/body, antes sin usar en este archivo.
    """
    if not fcm_token:
        print("No hay fcm_token registrado para este negocio, no se envia notificacion.")
        return

    try:
        _inicializar_firebase()

        mensaje = messaging.Message(
            notification=messaging.Notification(title=titulo, body=cuerpo),
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="citas_nuevas_v5",
                    priority="high",
                    default_sound=True,
                    icon="ic_stat_name",
                    color="#03A580",
                    image=LOGO_URL,
                ),
            ),
            data=data,
            token=fcm_token,
        )

        respuesta = messaging.send(mensaje)
        print(f"Notificacion push enviada correctamente: {respuesta}")
    except Exception as e:
        print(f"Error enviando notificacion push: {e}")


def enviar_notificacion_nueva_cita(fcm_token: str, nombre_cliente: str, servicio: str, fecha_hora_texto: str):
    """Notifica al negocio cuando se crea una cita nueva (por WhatsApp o manual)."""
    _enviar_push(
        fcm_token,
        titulo="Nueva cita agendada",
        cuerpo=f"{nombre_cliente} agendó {servicio} para {fecha_hora_texto}",
    )


def enviar_notificacion_cita_cancelada(fcm_token: str, nombre_cliente: str, servicio: str, fecha_hora_texto: str):
    """Notifica al negocio cuando un cliente cancela una cita por WhatsApp."""
    _enviar_push(
        fcm_token,
        titulo="Cita cancelada",
        cuerpo=f"{nombre_cliente} canceló su cita de {servicio} del {fecha_hora_texto}",
    )


def enviar_notificacion_cita_reprogramada(
    fcm_token: str, nombre_cliente: str, servicio: str, fecha_anterior_texto: str, fecha_nueva_texto: str
):
    """Notifica al negocio cuando un cliente reprograma (mueve) una cita por WhatsApp."""
    _enviar_push(
        fcm_token,
        titulo="Cita reprogramada",
        cuerpo=f"{nombre_cliente} movió su cita de {servicio} del {fecha_anterior_texto} al {fecha_nueva_texto}",
    )


def enviar_notificacion_pago_recibido(fcm_token: str, monto_texto: str, descripcion: str):
    """Notifica al negocio cuando Wompi confirma el pago de un abono solicitado por el chat."""
    _enviar_push(
        fcm_token,
        titulo="Pago recibido",
        cuerpo=f"Se confirmo un abono de {monto_texto} ({descripcion})",
    )


def enviar_notificacion_escalamiento(fcm_token: str, nombre_cliente: str, business_id: str, session_id: str):
    """
    Notifica al negocio que un cliente necesita atencion humana en el chat
    (el bot no entendio pese a intentar aclarar, o el cliente pidio
    explicitamente hablar con una persona). El payload `data` lleva
    business_id/session_id para que la app arme el link directo a esa
    conversacion (ver agent/tools.py:_notificar_negocio_escalamiento).
    """
    _enviar_push(
        fcm_token,
        titulo="Un cliente necesita ayuda",
        cuerpo=f"{nombre_cliente} esta esperando que alguien del equipo siga la conversacion.",
        data={"tipo": "chat_escalado", "business_id": business_id, "session_id": session_id},
    )

def enviar_notificacion_presupuesto_agotado(fcm_token: str, gasto_texto: str, presupuesto_texto: str):
    """
    Notifica al dueño que el asistente de IA se pauso porque el negocio llego a
    su presupuesto mensual (ver services/ai_usage_tracking.py:presupuesto_agotado).
    Es distinta de la alerta del 80 %: aqui el bot YA no responde a los clientes.
    """
    _enviar_push(
        fcm_token,
        titulo="Asistente de IA pausado",
        cuerpo=(
            f"Llegaste a tu presupuesto mensual de IA ({gasto_texto} de {presupuesto_texto}). "
            "Tus clientes veran un aviso con tu contacto hasta el proximo mes o hasta que subas el presupuesto."
        ),
        data={"tipo": "presupuesto_ia_agotado"},
    )
