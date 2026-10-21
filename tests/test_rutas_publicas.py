"""Chat publico, vista del negocio sobre conversaciones, webhooks, WebSocket, Baileys, tarjeta QR y endpoints de main.py."""

import asyncio
import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from starlette.websockets import WebSocketDisconnect

from tests.fakes import ModeloGuionado

BIZ = "biz-1"
INTERNA = {"x-api-key": "internal-test-key"}


@pytest.fixture
def negocio(db):
    db.sembrar("businesses", {"id": BIZ, "name": "Barberia", "business_type": "Barberia", "owner_id": "dueno", "plan": "pro",
                              "blocked": False, "phone_number": "3001234567", "fcm_token": "fcm", "whatsapp_phone_number_id": "pn-1"})
    db.sembrar("employees", {"id": "emp-1", "business_id": BIZ, "user_id": "dueno", "name": "Chenier", "role": "owner", "active": True},
               {"id": "emp-off", "business_id": BIZ, "user_id": "ex", "name": "Ex", "role": "staff", "active": False})


@pytest.fixture
def modelo(monkeypatch):
    from agent import graph

    guion = ModeloGuionado()
    monkeypatch.setattr(graph, "_model", guion)
    monkeypatch.setattr(graph, "_model_forzar_servicios", ModeloGuionado())
    return guion


# ------------------------------------------------------------------------------
# Chat publico (widget)
# ------------------------------------------------------------------------------

def test_config_y_sesion(cliente, negocio, db):
    config = cliente.get(f"/chat/{BIZ}/config").json()
    assert config == {"business_id": BIZ, "name": "Barberia", "business_type": "Barberia", "enabled": True, "employee_id": None, "employee_name": None}
    assert uuid.UUID(cliente.post(f"/chat/{BIZ}/sessions").json()["session_id"])
    assert cliente.get("/chat/nada/config").status_code == 404

    empleado = cliente.get(f"/chat/{BIZ}/emp-1/config").json()
    assert empleado["employee_name"] == "Chenier"
    assert uuid.UUID(cliente.post(f"/chat/{BIZ}/emp-1/sessions").json()["session_id"])
    assert cliente.get(f"/chat/{BIZ}/emp-off/config").status_code == 404
    assert cliente.get(f"/chat/{BIZ}/nadie/config").status_code == 404


@pytest.mark.parametrize("cambio", [{"plan": "none"}, {"blocked": True}, {"onboarding_completed": False}])
def test_negocio_deshabilitado_no_agenda(cliente, negocio, db, modelo, cambio):
    from agent.graph import MENSAJE_NEGOCIO_NO_DISPONIBLE

    db.filas("businesses")[0].update(cambio)
    sid = str(uuid.uuid4())
    assert cliente.get(f"/chat/{BIZ}/config").json()["enabled"] is False
    assert cliente.post(f"/chat/{BIZ}/sessions/{sid}/messages", json={"mensaje": "hola"}).json()["respuesta"] == MENSAJE_NEGOCIO_NO_DISPONIBLE
    assert cliente.post(f"/chat/{BIZ}/emp-1/sessions/{sid}/messages", json={"mensaje": "hola"}).json()["respuesta"] == MENSAJE_NEGOCIO_NO_DISPONIBLE
    assert modelo.llamadas == []


def test_enviar_mensaje_e_historial(cliente, negocio, modelo):
    sid = str(uuid.uuid4())
    modelo.respuestas = [AIMessage(content="¡Hola! ¿En qué te ayudo?"), AIMessage(content="Hola de nuevo")]
    r = cliente.post(f"/chat/{BIZ}/sessions/{sid}/messages", json={"mensaje": "hola", "client_message_id": "m-1"})
    assert r.json() == {"respuesta": "¡Hola! ¿En qué te ayudo?", "opciones": None}
    historial = cliente.get(f"/chat/{BIZ}/sessions/{sid}/messages").json()
    assert [m["role"] for m in historial["mensajes"]] == ["user", "assistant"]

    r = cliente.post(f"/chat/{BIZ}/emp-1/sessions/{sid}/messages", json={"mensaje": "hola otra vez"})
    assert r.json()["respuesta"] == "Hola de nuevo"
    assert len(cliente.get(f"/chat/{BIZ}/emp-1/sessions/{sid}/messages").json()["mensajes"]) == 4


def test_session_id_invalido_y_client_message_id_largo(cliente, negocio):
    assert cliente.post(f"/chat/{BIZ}/sessions/no-uuid/messages", json={"mensaje": "hola"}).status_code == 404
    assert cliente.get(f"/chat/{BIZ}/sessions/no-uuid/messages").status_code == 404
    sid = str(uuid.uuid4())
    assert cliente.post(f"/chat/{BIZ}/sessions/{sid}/messages", json={"mensaje": "x", "client_message_id": "a" * 65}).status_code == 422


def test_historial_con_base_caida_es_503(cliente, negocio, monkeypatch):
    from agent import graph

    monkeypatch.setattr(graph.GRAPH, "get_state", MagicMock(side_effect=RuntimeError("couldn't get a connection")))
    sid = str(uuid.uuid4())
    assert cliente.get(f"/chat/{BIZ}/sessions/{sid}/messages").status_code == 503
    assert cliente.get(f"/chat/{BIZ}/emp-1/sessions/{sid}/messages").status_code == 503


# ------------------------------------------------------------------------------
# Vista del negocio sobre una conversacion escalada
# ------------------------------------------------------------------------------

def test_negocio_lee_y_responde_una_conversacion(cliente, como, negocio, modelo):
    sid = str(uuid.uuid4())
    modelo.respuestas = [AIMessage(content="Dejame confirmar con el equipo")]
    cliente.post(f"/chat/{BIZ}/sessions/{sid}/messages", json={"mensaje": "necesito algo raro"})

    url = f"/business/{BIZ}/chat-sessions/{sid}"
    assert len(cliente.get(url, headers=como("dueno")).json()["mensajes"]) == 2
    r = cliente.post(f"{url}/reply", json={"mensaje": "Hola, soy Chenier"}, headers=como("dueno"))
    assert r.json()["mensajes"][-1] == {"role": "assistant", "content": "Hola, soy Chenier"}
    # Desde aqui el bot ya no responde: lo atiende el humano.
    from agent.graph import MENSAJE_TRANSFERIDO

    assert cliente.post(f"/chat/{BIZ}/sessions/{sid}/messages", json={"mensaje": "gracias"}).json()["respuesta"] == MENSAJE_TRANSFERIDO
    assert cliente.get(url, headers=como("intruso")).status_code == 403
    assert cliente.get(url).status_code == 401


# ------------------------------------------------------------------------------
# Webhook de WhatsApp (Meta) y webhooks internos
# ------------------------------------------------------------------------------

from routes import webhook  # noqa: E402


def mensaje_meta(texto="hola", phone_number_id="pn-1", de="573001234567"):
    mensaje = {"from": de, **({"text": {"body": texto}} if texto is not None else {"type": "audio"})}
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": phone_number_id}, "messages": [mensaje]}}]}]}


def test_verificacion_del_webhook(cliente):
    assert cliente.get("/webhook?hub.mode=subscribe&hub.verify_token=verify-test&hub.challenge=123").json() == 123
    assert cliente.get("/webhook?hub.mode=subscribe&hub.verify_token=otro&hub.challenge=123").json() == {"error": "Verificacion fallida"}


def test_mensaje_entrante_registra_la_ventana_de_24h(cliente, negocio, db):
    assert cliente.post("/webhook", json=mensaje_meta()).json() == {"status": "ok"}
    assert db.filas("whatsapp_client_contacts")[0]["client_phone"] == "573001234567"


def test_frase_de_confirmacion_confirma_la_cita_web(cliente, negocio, db):
    pendiente = db.sembrar("appointments", {"business_id": BIZ, "client_phone": "573001234567", "status": "pending"})
    texto = "Hola, Acabo de agendar una cita a través de la web"
    cliente.post("/webhook", json=mensaje_meta(texto))
    assert pendiente["status"] == "confirmed"


def test_frase_de_confirmacion_con_error_no_tumba_el_webhook(cliente, negocio, db, monkeypatch, capsys):
    from tests.fakes import _Consulta

    original = _Consulta.execute

    def falla_update(self):
        if self._tabla == "appointments" and self._operacion == "update":
            raise RuntimeError("caido")
        return original(self)

    monkeypatch.setattr(_Consulta, "execute", falla_update)
    assert cliente.post("/webhook", json=mensaje_meta("Acabo de agendar una cita a través de la web")).status_code == 200
    assert "Error confirmando cita web" in capsys.readouterr().out


@pytest.mark.parametrize("cuerpo", [
    {"entry": [{"changes": [{"value": {"statuses": []}}]}]},  # estado de entrega, no mensaje
    mensaje_meta(phone_number_id="desconocido"),
    mensaje_meta(texto=None),  # audio
    {"entry": []},  # payload raro
])
def test_webhook_casos_que_se_ignoran(cliente, negocio, cuerpo):
    assert cliente.post("/webhook", json=cuerpo).json() == {"status": "ok"}


def test_firma_de_meta(cliente, negocio, monkeypatch):
    monkeypatch.setattr(webhook, "META_APP_SECRET", "secreto")
    cuerpo = json.dumps(mensaje_meta()).encode()
    firma = "sha256=" + hmac.new(b"secreto", cuerpo, hashlib.sha256).hexdigest()
    cabeceras = {"content-type": "application/json"}
    assert cliente.post("/webhook", content=cuerpo, headers={**cabeceras, "X-Hub-Signature-256": firma}).status_code == 200
    assert cliente.post("/webhook", content=cuerpo, headers={**cabeceras, "X-Hub-Signature-256": "sha256=falsa"}).status_code == 401
    assert cliente.post("/webhook", content=cuerpo, headers=cabeceras).status_code == 401


@pytest.fixture
def efectos_webhook(monkeypatch):
    from services import push_notifications, realtime

    efectos = MagicMock()
    monkeypatch.setattr(push_notifications, "enviar_notificacion_nueva_cita", efectos.push)
    monkeypatch.setattr(realtime, "emitir_evento_cita", efectos.evento)
    return efectos


def cita_web(db, **extra):
    return db.sembrar("appointments", {"business_id": BIZ, "client_name": "Ana", "status": "pending", "scheduled_at": "2026-10-10T10:00:00",
                                       "services": {"name": "Corte"}, "businesses": {"fcm_token": "fcm"}, **extra})


def test_notify_web_booking(cliente, negocio, db, efectos_webhook):
    cita = cita_web(db)
    assert cliente.post("/notify-web-booking", json={"appointment_id": cita["id"], "business_id": BIZ}).status_code == 401
    r = cliente.post("/notify-web-booking", json={"appointment_id": cita["id"], "business_id": BIZ}, headers=INTERNA)
    assert r.json()["status"] == "ok"
    assert efectos_webhook.push.call_args.kwargs["fecha_hora_texto"] == "2026-10-10 10:00"
    efectos_webhook.evento.assert_called_once_with("appointment.created", BIZ, cita["id"])
    assert cliente.post("/notify-web-booking", json={"appointment_id": "nada", "business_id": BIZ}, headers=INTERNA).json() == {"error": "Cita no encontrada"}


def test_notify_web_booking_fecha_rara_y_push_que_falla(cliente, negocio, db, efectos_webhook, capsys):
    cita = cita_web(db, scheduled_at="pronto")
    efectos_webhook.push.side_effect = RuntimeError("firebase")
    cliente.post("/notify-web-booking", json={"appointment_id": cita["id"], "business_id": BIZ}, headers=INTERNA)
    assert efectos_webhook.push.call_args.kwargs["fecha_hora_texto"] == "pronto"
    assert "Error enviando push notification desde web" in capsys.readouterr().out
    sin_token = cita_web(db, businesses={"fcm_token": None})
    efectos_webhook.push.reset_mock()
    cliente.post("/notify-web-booking", json={"appointment_id": sin_token["id"], "business_id": BIZ}, headers=INTERNA)
    efectos_webhook.push.assert_not_called()


def test_supabase_webhook(cliente, negocio, db, efectos_webhook, capsys):
    cita = cita_web(db)
    base = {"type": "INSERT", "table": "appointments", "record": {"id": cita["id"], "business_id": BIZ, "status": "pending"}}
    assert cliente.post("/supabase-webhook", json=base, headers=INTERNA).json()["status"] == "ok"
    efectos_webhook.evento.assert_called_once()
    assert cliente.post("/supabase-webhook", json={**base, "type": "UPDATE"}, headers=INTERNA).json()["status"] == "ignorado"
    assert cliente.post("/supabase-webhook", json={**base, "record": {**base["record"], "status": "confirmed"}}, headers=INTERNA).json()["status"] == "ignorado"
    assert "error" in cliente.post("/supabase-webhook", json={**base, "record": {**base["record"], "id": "nada"}}, headers=INTERNA).json()

    rara = cita_web(db, scheduled_at="pronto")
    efectos_webhook.push.side_effect = RuntimeError("firebase")
    cliente.post("/supabase-webhook", json={**base, "record": {**base["record"], "id": rara["id"]}}, headers=INTERNA)
    assert "Error enviando push notification desde supabase webhook" in capsys.readouterr().out
    sin_token = cita_web(db, businesses={})
    efectos_webhook.push.reset_mock()
    cliente.post("/supabase-webhook", json={**base, "record": {**base["record"], "id": sin_token["id"]}}, headers=INTERNA)
    efectos_webhook.push.assert_not_called()


# ------------------------------------------------------------------------------
# WebSocket del panel
# ------------------------------------------------------------------------------

def test_websocket_autenticado_recibe_eventos(cliente, como, negocio, db):
    from services.realtime import gestor_tiempo_real

    como("dueno")
    with cliente.websocket_connect(f"/ws/appointments?business_id={BIZ}") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "tok-dueno"}))
        assert ws.receive_json() == {"type": "auth_ok"}
        gestor_tiempo_real.emitir(BIZ, {"type": "appointment.created"})
        assert ws.receive_json() == {"type": "appointment.created"}
        ws.send_text("cualquier cosa se ignora")
    assert BIZ not in gestor_tiempo_real._conexiones


@pytest.mark.parametrize("primer_mensaje, codigo", [
    ("no-es-json", 4400),
    (json.dumps({"type": "auth"}), 4401),
    (json.dumps(["lista"]), 4401),
    (json.dumps({"type": "auth", "token": "invalido"}), 4401),
    (json.dumps({"type": "auth", "token": "tok-intruso"}), 4403),
])
def test_websocket_rechaza_sin_autenticacion_valida(cliente, como, negocio, primer_mensaje, codigo):
    como("intruso")
    with cliente.websocket_connect(f"/ws/appointments?business_id={BIZ}") as ws:
        ws.send_text(primer_mensaje)
        with pytest.raises(WebSocketDisconnect) as cierre:
            ws.receive_text()
    assert cierre.value.code == codigo


def test_websocket_sin_auth_a_tiempo(cliente, negocio, monkeypatch):
    from routes import realtime_routes

    async def nunca_llega(corrutina, timeout):
        corrutina.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(realtime_routes, "asyncio", SimpleNamespace(wait_for=nunca_llega, TimeoutError=asyncio.TimeoutError))
    with cliente.websocket_connect(f"/ws/appointments?business_id={BIZ}") as ws:
        with pytest.raises(WebSocketDisconnect) as cierre:
            ws.receive_text()
    assert cierre.value.code == 4408


def test_resolver_usuario_del_websocket(db, monkeypatch):
    from routes.realtime_routes import _resolver_usuario

    assert _resolver_usuario(None) is None
    monkeypatch.setattr(db.auth, "get_user", lambda t: SimpleNamespace(user=None))
    assert _resolver_usuario("tok") is None


# ------------------------------------------------------------------------------
# Baileys
# ------------------------------------------------------------------------------

def test_baileys(cliente, como, negocio, monkeypatch):
    from routes import baileys_routes

    assert cliente.post("/baileys/message", json={"business_id": BIZ, "client_phone": "573", "mensaje": "hola"}, headers=INTERNA).json() == {"respuesta_ia": None}
    assert "error" in cliente.post("/baileys/message", json={"business_id": "nada", "client_phone": "573", "mensaje": "x"}, headers=INTERNA).json()

    monkeypatch.setattr(baileys_routes, "solicitar_pairing_code", lambda b, tel: {"code": "ABCD-1234", "tel": tel})
    monkeypatch.setattr(baileys_routes, "estado_conexion", lambda b: {"connected": True})
    assert cliente.post("/baileys/pairing-code", json={"business_id": BIZ, "phone": "+57 300 123 4567"}, headers=como("dueno")).json()["tel"] == "573001234567"
    assert "Numero invalido" in cliente.post("/baileys/pairing-code", json={"business_id": BIZ, "phone": "123"}, headers=como("dueno")).json()["error"]
    assert cliente.get(f"/baileys/status?business_id={BIZ}", headers=como("dueno")).json() == {"connected": True}

    def sin_servicio(*a):
        raise requests.exceptions.ConnectionError("baileys caido")

    monkeypatch.setattr(baileys_routes, "solicitar_pairing_code", sin_servicio)
    monkeypatch.setattr(baileys_routes, "estado_conexion", sin_servicio)
    assert "No se pudo contactar" in cliente.post("/baileys/pairing-code", json={"business_id": BIZ, "phone": "3001234567"}, headers=como("dueno")).json()["error"]
    assert "No se pudo contactar" in cliente.get(f"/baileys/status?business_id={BIZ}", headers=como("dueno")).json()["error"]


# ------------------------------------------------------------------------------
# Tarjeta QR
# ------------------------------------------------------------------------------

def test_tarjeta_qr_es_un_png_real(cliente, como, negocio):
    r = cliente.post("/qr-card", json={"business_id": BIZ, "chat_link": "https://app.goagenda.online/chat/biz-1"}, headers=como("dueno"))
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_tarjeta_qr_con_nombre_muy_largo():
    from services.qr_card import generar_tarjeta_qr

    assert generar_tarjeta_qr("https://x.co", "Centro de estética y spa Alejandra Castaño " * 3)[:4] == b"\x89PNG"


def test_tarjeta_qr_validaciones(cliente, como, negocio, db, monkeypatch):
    from routes import qr_card_routes

    assert cliente.post("/qr-card", json={"business_id": BIZ, "chat_link": "  "}, headers=como("dueno")).status_code == 400
    assert cliente.post("/qr-card", json={"business_id": BIZ, "chat_link": "x"}, headers=como("intruso")).status_code == 403
    db.filas("businesses")[0]["name"] = "  "
    assert cliente.post("/qr-card", json={"business_id": BIZ, "chat_link": "x"}, headers=como("dueno")).status_code == 400
    monkeypatch.setattr(qr_card_routes, "verificar_acceso_negocio", lambda b, u: None)
    assert cliente.post("/qr-card", json={"business_id": "nada", "chat_link": "x"}, headers=como("dueno")).status_code == 404


# ------------------------------------------------------------------------------
# Endpoints de main.py
# ------------------------------------------------------------------------------

def test_raiz_y_test_db(cliente, negocio):
    assert cliente.get("/").json()["status"].startswith("GoAgenda")
    assert cliente.get("/test-db").status_code == 401
    assert cliente.get("/test-db", headers=INTERNA).json()["businesses"][0]["id"] == BIZ


def test_origenes_cors(monkeypatch):
    import main

    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.co, https://b.co ,")
    assert main._obtener_origenes_cors() == ["https://a.co", "https://b.co"]
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")
    assert main._obtener_origenes_cors() == ["http://localhost:4200", "http://127.0.0.1:4200"]


def test_gasto_de_ia(cliente, como, negocio, db, monkeypatch):
    import main

    db.sembrar("ai_usage_log", {"business_id": BIZ, "estimated_cost_usd": 1.0, "created_at": "2099-01-01"})
    r = cliente.get(f"/ai-usage?business_id={BIZ}", headers=como("dueno")).json()
    assert r["presupuesto_mensual_usd"] == 5.0 and r["gasto_actual_usd"] == 1.0 and r["porcentaje_usado"] == 20.0
    monkeypatch.setattr(main, "verificar_dueno", lambda b, u: None)
    assert cliente.get("/ai-usage?business_id=nada", headers=como("dueno")).json() == {"error": "Negocio no encontrado"}


def test_endpoints_internos_de_prueba(cliente, negocio, db, monkeypatch):
    from services import push_notifications, whatsapp
    import main

    push = MagicMock()
    monkeypatch.setattr(push_notifications, "enviar_notificacion_nueva_cita", push)
    monkeypatch.setattr(whatsapp, "enviar_confirmacion_cita_cliente", lambda **kw: {"ok": True})
    monkeypatch.setattr(whatsapp, "enviar_recordatorio_cita_template", lambda **kw: {"ok": kw["nombre_negocio"]})
    monkeypatch.setattr(main, "revisar_y_enviar_recordatorios", lambda: 3)

    assert cliente.post(f"/test-push?business_id={BIZ}", headers=INTERNA).json()["status"].startswith("Notificacion enviada")
    push.assert_called_once()
    assert "error" in cliente.post("/test-push?business_id=nada", headers=INTERNA).json()
    db.filas("businesses")[0]["fcm_token"] = None
    assert "fcm_token" in cliente.post(f"/test-push?business_id={BIZ}", headers=INTERNA).json()["error"]

    assert cliente.post("/test-whatsapp-confirmation?numero_whatsapp=3001234567", headers=INTERNA).json()["enviado_a"] == "573001234567"
    assert "error" in cliente.post("/test-whatsapp-confirmation?numero_whatsapp=123", headers=INTERNA).json()
    r = cliente.post(f"/test-whatsapp-reminder-template?business_id={BIZ}&numero_whatsapp=3001234567", headers=INTERNA).json()
    assert r["resultado"] == {"ok": "Barberia"}
    assert "error" in cliente.post(f"/test-whatsapp-reminder-template?business_id={BIZ}&numero_whatsapp=1", headers=INTERNA).json()
    assert "error" in cliente.post("/test-whatsapp-reminder-template?business_id=nada&numero_whatsapp=3001234567", headers=INTERNA).json()
    assert cliente.post("/test-reminders", headers=INTERNA).json() == {"recordatorios_enviados": 3}


def test_scheduler_registra_los_jobs():
    """Modo "api" (por defecto): el APScheduler de la API ejecuta cada tarea directamente."""
    import main
    from services.reminder_service import revisar_y_enviar_recordatorios
    from services.retencion_conversaciones import limpiar_conversaciones_vencidas
    from services.wompi_payment_requests import expirar_solicitudes_vencidas

    jobs = {job[0]: (job[1], job[2]) for job in main.scheduler.jobs}
    assert jobs[revisar_y_enviar_recordatorios][0] == ("interval",)
    assert jobs[revisar_y_enviar_recordatorios][1]["minutes"] == 5
    assert jobs[expirar_solicitudes_vencidas][1]["minutes"] == 15
    assert jobs[limpiar_conversaciones_vencidas][0] == ("cron",)
    assert jobs[limpiar_conversaciones_vencidas][1]["hour"] == 8 and jobs[limpiar_conversaciones_vencidas][1]["misfire_grace_time"] == 3600
