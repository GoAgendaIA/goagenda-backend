"""
Bug 4: el chatbot no debe consultar ni mencionar citas anteriores del cliente cuando este
solo escribe algo suelto como "turno" o "cita" (intencion de agendar una nueva).
"""

import json
import uuid

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from agent import tools as t
from tests.test_fase3 import BIZ, enviar, hora_fija, llamada, modelos, negocio, texto  # noqa: F401


def humano(*textos):
    return [HumanMessage(content=x) for x in textos]


# --- el detector --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mensaje", [
    "turno", "Turno", "cita", "Cita", "una cita", "quiero una cita", "quiero agendar", "necesito un turno",
    "hola", "para el viernes", "corte de cabello", "quiero sacar una cita para mañana", "agendar cita",
])
def test_mensajes_sueltos_no_piden_consultar_citas_existentes(mensaje):
    assert not t._cliente_pidio_sus_citas(humano(mensaje))


@pytest.mark.parametrize("mensaje", [
    "¿Cuándo fue mi última cita?", "quiero cancelar mi cita", "Cancela la cita del viernes", "necesito reprogramar",
    "quiero mover mi cita", "cambiar mi cita para otro día", "¿qué citas tengo?", "mis citas", "tengo una cita mañana?",
    "¿a qué hora es mi cita?", "ver mi cita", "consultar mi reserva", "mi próxima cita", "quiero reagendar",
    "ya tengo una cita agendada", "¿cuándo es mi turno?",
])
def test_peticiones_explicitas_si_permiten_consultar(mensaje):
    assert t._cliente_pidio_sus_citas(humano(mensaje))


def test_la_respuesta_a_una_pregunta_del_bot_hereda_la_intencion_reciente():
    assert t._cliente_pidio_sus_citas(humano("quiero cancelar mi cita", "la del viernes"))


def test_una_peticion_vieja_ya_no_cuenta():
    antiguos = ["quiero cancelar mi cita"] + ["corte"] * 4
    assert not t._cliente_pidio_sus_citas(humano(*antiguos))


def test_sin_estado_no_bloquea():
    assert t._cliente_pidio_sus_citas(None)


# --- la herramienta ------------------------------------------------------------------------------------------

@pytest.fixture
def cita_pasada(negocio, db):
    return db.sembrar("appointments", {
        "business_id": BIZ, "employee_id": "emp-1", "client_phone": "573001234567", "client_name": "Ana",
        "service_id": "s1", "scheduled_at": "2026-09-01T10:00:00", "status": "confirmed",
        "services": {"name": "Corte de cabello", "duration_minutes": 30},
    })


def test_con_un_mensaje_suelto_no_devuelve_citas(cita_pasada):
    r = t.consultar_citas_cliente.func(BIZ, "573001234567", humano("turno"))
    assert r["citas_no_consultadas"] is True and "citas" not in r
    assert "AGENDAR una nueva" in r["instruccion"]
    assert "2026-09-01" not in json.dumps(r) and "Corte de cabello" not in json.dumps(r)


def test_con_una_peticion_explicita_devuelve_las_citas(cita_pasada):
    r = t.consultar_citas_cliente.func(BIZ, "573001234567", humano("¿cuándo fue mi última cita?"))
    assert [c["id"] for c in r["citas"]] == [cita_pasada["id"]]


def test_sin_telefono_pide_el_whatsapp_antes(cita_pasada):
    assert "WhatsApp" in t.consultar_citas_cliente.func(BIZ, None, humano("turno"))["error"]


# --- el chat completo --------------------------------------------------------------------------------------------

def _resultados(sid):
    from agent import graph as g

    mensajes = g.GRAPH.get_state(g._thread_config(BIZ, sid)).values["messages"]
    return [m for m in mensajes if isinstance(m, ToolMessage) and m.name == "consultar_citas_cliente"]


def test_en_el_chat_un_turno_suelto_no_filtra_la_cita_anterior(cita_pasada, modelos):
    sid = str(uuid.uuid4())
    modelos.principal.respuestas = [
        llamada("registrar_telefono_cliente", {"numero_whatsapp": "3001234567"}),
        texto("Listo, ya te identifiqué. ¿En qué te ayudo?"),
        # Un modelo que, ante "turno", consulta citas por su cuenta (el bug).
        llamada("consultar_citas_cliente"),
        texto("¡Claro! 😊 ¿Para qué día te gustaría agendar tu turno?"),
    ]
    enviar("mi whatsapp es 3001234567", sid)
    respuesta, _ = enviar("turno", sid)

    resultado = _resultados(sid)[0]
    assert "citas_no_consultadas" in resultado.content and "2026-09-01" not in resultado.content
    assert "2026" not in respuesta and "septiembre" not in respuesta


def test_en_el_chat_una_peticion_explicita_si_ve_sus_citas(cita_pasada, modelos):
    sid = str(uuid.uuid4())
    modelos.principal.respuestas = [
        llamada("registrar_telefono_cliente", {"numero_whatsapp": "3001234567"}),
        texto("Listo."),
        llamada("consultar_citas_cliente"),
        texto("Tu última cita fue el 1 de septiembre."),
    ]
    enviar("mi whatsapp es 3001234567", sid)
    enviar("¿Cuándo fue mi última cita?", sid)
    assert "2026-09-01" in _resultados(sid)[0].content
