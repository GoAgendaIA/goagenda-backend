import asyncio

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from postgrest.exceptions import APIError
import atexit
import os

from services.auth import obtener_usuario_actual, verificar_dueno, requiere_api_key_interna
from services.seed_super_admin import sembrar_super_admin_por_defecto
from services.realtime import gestor_tiempo_real

from routes.webhook import router as webhook_router
from routes.chat_routes import router as chat_router, business_chat_router
from routes.services_routes import router as services_router
from routes.business_hours_routes import router as business_hours_router
from routes.business_settings_routes import router as business_settings_router
from services.db import supabase
from services.reminder_service import revisar_y_enviar_recordatorios
from core.settings import CHAT_LIMPIEZA_HORA_UTC
from services.retencion_conversaciones import limpiar_conversaciones_vencidas
from routes.manual_appointments import router as manual_appointments_router
from routes.baileys_routes import router as baileys_router
from routes.excluded_chats_routes import router as excluded_chats_router
from routes.qr_card_routes import router as qr_card_router
from routes.admin_routes import router as admin_router
from routes.metrics_routes import router as metrics_router
from routes.invitation_codes_routes import router as invitation_codes_router
from routes.employees_routes import router as employees_router
from routes.me_routes import router as me_router
from routes.realtime_routes import router as realtime_router
from routes.home_visit_zones_routes import router as home_visit_zones_router
from routes.wompi_settings_routes import router as wompi_settings_router
from routes.wompi_payment_requests_routes import router as wompi_payment_requests_router
from routes.wompi_webhook_routes import router as wompi_webhook_router
from services.wompi_payment_requests import expirar_solicitudes_vencidas


load_dotenv()

from fastapi.middleware.cors import CORSMiddleware

def _obtener_origenes_cors() -> list[str]:
    """Lee CORS_ALLOWED_ORIGINS (coma-separado) y aplica defaults de desarrollo."""
    valor_env = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
    if valor_env:
        return [o.strip() for o in valor_env.split(",") if o.strip()]

    return [
        "http://localhost:4200",
        "http://127.0.0.1:4200",
    ]


# Controla el orden y la descripcion de las secciones en /docs; sin esto,
# cualquier router sin `tags` (o endpoint declarado directo en `app`) cae en
# una seccion generica "default" que no dice nada sobre que hace cada cosa.
TAGS_METADATA = [
    {"name": "auth", "description": "Sesion del usuario logueado: rol (super admin) y en que negocio(s) tiene un registro de empleado."},
    {"name": "chat", "description": "Chat publico del agente de IA (sin auth). El enlace es la unica credencial; ver la seccion 'Chat API' del README."},
    {"name": "services", "description": "Servicios que ofrece un negocio (nombre, precio, duracion)."},
    {"name": "employees", "description": "Empleados de un negocio: datos, horario propio y servicios a los que estan ligados. El dueño es tambien un empleado (el 'empleado principal')."},
    {"name": "appointments", "description": "Citas creadas manualmente desde el panel (no por el chat), ej. cliente presencial o por telefono."},
    {"name": "business-hours", "description": "Horario general del negocio (informativo; el horario real de agendamiento es el de cada empleado)."},
    {"name": "business-settings", "description": "Configuracion general del negocio: nombre, WhatsApp, token push, recordatorios y gasto de IA."},
    {"name": "excluded-chats", "description": "Numeros excluidos de las respuestas automaticas del bot para un negocio."},
    {"name": "qr-card", "description": "Generacion de la tarjeta QR imprimible con el enlace del chat de un negocio."},
    {"name": "invitations", "description": "Codigos de invitacion de 6 caracteres: el dueño invita empleados, y cualquiera los valida para quedar ligado a un negocio."},
    {"name": "metricas", "description": "Centro de metricas y analitica (solo super admin): panel global de la plataforma y panel individual por negocio, con filtros por periodo, origen IA/manual, estados, conversion y alertas."},
    {"name": "super-admin", "description": "Endpoints exclusivos del super admin: crear negocios, bloquearlos, y generar sus codigos de invitacion de dueño."},
    {"name": "whatsapp", "description": "Integracion saliente de WhatsApp (Meta Cloud API y el microservicio Baileys): recordatorios y confirmacion de citas. El bot conversacional ya no responde por aqui."},
    {"name": "realtime", "description": "WebSocket de notificaciones de citas en tiempo real para el panel (ver README, seccion 'WebSocket de tiempo real'). No aparece en este /docs porque OpenAPI no documenta WebSockets."},
    {"name": "wompi", "description": "Configuracion de la pasarela de pagos Wompi por negocio (credenciales encriptadas, multi-tenant) y links de pago generados desde el chat."},
    {"name": "sistema", "description": "Endpoints generales del servicio."},
    {"name": "herramientas-internas", "description": "Endpoints de prueba/operacion protegidos con la api key interna (no para el frontend)."},
]

app = FastAPI(title="GoAgenda API", openapi_tags=TAGS_METADATA)

ORIGENES_CORS = _obtener_origenes_cors()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES_CORS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook_router)
# Chat publico del agente de IA: sin auth, aislado por business_id en la URL
# (ver routes/chat_routes.py). El enlace que el negocio comparte con sus
# clientes es la unica "credencial".
app.include_router(chat_router)
# Vista autenticada del negocio sobre una conversacion escalada (ver
# routes/chat_routes.py, seccion "Vista del negocio sobre una conversacion").
app.include_router(business_chat_router)
app.include_router(services_router)
app.include_router(business_hours_router)
app.include_router(business_settings_router)
app.include_router(manual_appointments_router)
app.include_router(baileys_router)
app.include_router(excluded_chats_router)
app.include_router(qr_card_router)
app.include_router(admin_router)
app.include_router(metrics_router)
app.include_router(invitation_codes_router)
app.include_router(employees_router)
app.include_router(me_router)
app.include_router(realtime_router)
app.include_router(home_visit_zones_router)
app.include_router(wompi_settings_router)
app.include_router(wompi_payment_requests_router)
# Webhook de Wompi: publico (sin auth de usuario), protegido por la firma
# SHA256 del secreto de eventos de cada negocio (ver routes/wompi_webhook_routes.py).
app.include_router(wompi_webhook_router)


@app.on_event("startup")
async def _registrar_loop_tiempo_real() -> None:
    """
    Le da al gestor de conexiones WS una referencia al event loop principal,
    para que `emitir_evento_cita` (llamado desde codigo sync que corre en el
    threadpool, ej. agent/tools.py) pueda programar el envio ahi en vez de
    intentar hacer await desde un hilo que no es el del loop.
    """
    gestor_tiempo_real.registrar_loop(asyncio.get_running_loop())


@app.exception_handler(APIError)
async def manejar_error_postgrest(request: Request, exc: APIError):
    """
    Traduce errores crudos de Postgrest a respuestas HTTP claras, en vez de
    dejar que revienten como 500 con un stacktrace opaco:
    - 22P02 (invalid_text_representation): un id en la URL (business_id,
      employee_id, etc) no tiene formato de UUID valido -> 404, igual que
      si el recurso simplemente no existiera.
    - PGRST205 (tabla no encontrada en el schema cache de PostgREST): falta
      correr una migracion .sql de la raiz del repo contra la base de
      datos, o recargar el schema cache de PostgREST despues de correrla.
    """
    if exc.code == "22P02":
        return JSONResponse(status_code=404, content={"detail": "Recurso no encontrado"})
    if exc.code == "PGRST205":
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Falta una tabla en la base de datos. Corre las migraciones .sql de la "
                    "raiz del repo (ver roles_empleados_invitaciones.sql) en el SQL Editor de "
                    "Supabase, y si el error persiste recarga el schema cache de PostgREST."
                )
            },
        )
    print(f"Error de Postgrest sin manejar ({exc.code}): {exc}")
    return JSONResponse(status_code=500, content={"detail": "Error interno"})


# Crea (si no existe) el super admin por defecto a partir de
# SUPER_ADMIN_EMAIL/SUPER_ADMIN_PASSWORD en el .env; no hace nada si esas
# variables no estan configuradas. Ver services/seed_super_admin.py.
sembrar_super_admin_por_defecto()


@app.get("/", tags=["sistema"])
def root():
    """Chequeo rapido de que el backend esta corriendo (para monitoreo/uptime)."""
    return {"status": "GoAgenda backend corriendo correctamente"}


@app.get("/test-db", tags=["herramientas-internas"], dependencies=[Depends(requiere_api_key_interna)])
def test_db():
    """Endpoint de prueba para confirmar que la conexion a Supabase funciona."""
    response = supabase.table("businesses").select("*").execute()
    return {"businesses": response.data}

@app.get("/ai-usage", tags=["business-settings"])
def consultar_gasto_ia(business_id: str, user_id: str = Depends(obtener_usuario_actual)):
    """Consulta el gasto estimado de IA del negocio en el mes actual."""
    verificar_dueno(business_id, user_id)
    from services.ai_usage_tracking import obtener_gasto_mensual

    business_response = supabase.table("businesses").select("monthly_ai_budget_usd, name").eq("id", business_id).execute()
    if not business_response.data:
        return {"error": "Negocio no encontrado"}

    presupuesto = business_response.data[0].get("monthly_ai_budget_usd") or 5.00
    gasto = obtener_gasto_mensual(business_id)

    return {
        "negocio": business_response.data[0]["name"],
        "gasto_actual_usd": gasto,
        "presupuesto_mensual_usd": presupuesto,
        "porcentaje_usado": round((gasto / presupuesto) * 100, 1) if presupuesto > 0 else 0,
    }

@app.post("/test-push", tags=["herramientas-internas"], dependencies=[Depends(requiere_api_key_interna)])
def test_push(business_id: str):
    """Prueba el envio de notificacion push sin pasar por la IA (no gasta cuota de Gemini)."""
    from services.push_notifications import enviar_notificacion_nueva_cita

    response = supabase.table("businesses").select("*").eq("id", business_id).execute()
    if not response.data:
        return {"error": "Negocio no encontrado"}

    business = response.data[0]
    fcm_token = business.get("fcm_token")

    if not fcm_token:
        return {"error": "Este negocio no tiene fcm_token registrado todavia. Abre la app primero."}

    enviar_notificacion_nueva_cita(
        fcm_token=fcm_token,
        nombre_cliente="Cliente de Prueba",
        servicio="Corte de cabello",
        fecha_hora_texto="hoy a las 15:00",
    )
    return {"status": "Notificacion enviada (revisa tu celular)"}


@app.post("/test-whatsapp-confirmation", tags=["herramientas-internas"], dependencies=[Depends(requiere_api_key_interna)])
def test_whatsapp_confirmation(numero_whatsapp: str):
    """
    Prueba el envio de la confirmacion de cita por WhatsApp (numero
    compartido de GoAgenda, ver services/whatsapp.py) a un numero real, sin
    tener que agendar una cita de verdad. Util para verificar en caliente si
    Meta esta aceptando el mensaje de texto libre o si hace falta un
    message template (ver README, seccion "Notificaciones de WhatsApp al
    cliente"). El numero se normaliza igual que en el chat/panel.
    """
    from services.whatsapp import enviar_confirmacion_cita_cliente, normalizar_numero_whatsapp

    numero_normalizado = normalizar_numero_whatsapp(numero_whatsapp)
    if not numero_normalizado:
        return {"error": "Ese numero no parece un WhatsApp colombiano valido (celular de 10 digitos que empieza en 3)."}

    resultado = enviar_confirmacion_cita_cliente(
        client_phone=numero_normalizado,
        nombre_cliente="Cliente de Prueba",
        nombre_negocio="Negocio de Prueba",
        nombre_servicio="Corte de cabello",
        fecha_hora_texto="hoy a las 15:00",
    )
    return {"enviado_a": numero_normalizado, "resultado": resultado}


@app.post("/test-whatsapp-reminder-template", tags=["herramientas-internas"], dependencies=[Depends(requiere_api_key_interna)])
def test_whatsapp_reminder_template(business_id: str, numero_whatsapp: str):
    """
    Prueba el envio del recordatorio de cita como message template (ver
    services/whatsapp.py:enviar_recordatorio_cita_template) a un numero
    real, sin esperar a que el scheduler encuentre una cita real dentro de
    su ventana de recordatorio. Igual que la confirmacion, sale del numero
    propio de GoAgenda; business_id solo aporta el nombre del negocio.
    """
    from services.whatsapp import enviar_recordatorio_cita_template, normalizar_numero_whatsapp

    numero_normalizado = normalizar_numero_whatsapp(numero_whatsapp)
    if not numero_normalizado:
        return {"error": "Ese numero no parece un WhatsApp colombiano valido (celular de 10 digitos que empieza en 3)."}

    business_response = supabase.table("businesses").select("name").eq("id", business_id).execute()
    if not business_response.data:
        return {"error": "Negocio no encontrado"}

    business = business_response.data[0]

    resultado = enviar_recordatorio_cita_template(
        to=numero_normalizado,
        nombre_cliente="Cliente de Prueba",
        nombre_negocio=business["name"],
        nombre_servicio="Corte de cabello",
        fecha_hora_texto="hoy a las 15:00",
    )
    return {"enviado_a": numero_normalizado, "resultado": resultado}


@app.post("/test-reminders", tags=["herramientas-internas"], dependencies=[Depends(requiere_api_key_interna)])
def test_reminders():
    """Endpoint para probar manualmente el envio de recordatorios sin esperar al scheduler."""
    enviados = revisar_y_enviar_recordatorios()
    return {"recordatorios_enviados": enviados}


# ---------------------------------------------------------
# Scheduler: revisa cada 5 minutos si hay recordatorios pendientes, y cada
# 15 minutos expira las solicitudes de pago de Wompi vencidas (ver
# services/wompi_payment_requests.py:expirar_solicitudes_vencidas).
# ---------------------------------------------------------
scheduler = BackgroundScheduler()
scheduler.add_job(revisar_y_enviar_recordatorios, "interval", minutes=5)
scheduler.add_job(expirar_solicitudes_vencidas, "interval", minutes=15)
# Retencion de conversaciones: una vez al dia (la hora se fija aparte del plazo). El cron y
# no un intervalo, porque cada despliegue reinicia el proceso y un intervalo de 24 h nunca
# llegaria a cumplirse; misfire_grace_time lo deja correr si el proceso estaba reiniciando.
scheduler.add_job(
    limpiar_conversaciones_vencidas, "cron", hour=CHAT_LIMPIEZA_HORA_UTC, minute=0, misfire_grace_time=3600
)
scheduler.start()

atexit.register(lambda: scheduler.shutdown())