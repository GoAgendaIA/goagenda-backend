"""
Fase 4 de la optimizacion de tokens: preguntas frecuentes (horario y servicios/precios)
respondidas en codigo sin llamar al modelo (T4.1) y su medicion como KPI.
"""

import uuid

import pytest

from agent import flujos
from agent import graph as g
from tests.test_fase3 import (  # noqa: F401  (fixtures y ayudas compartidas con la Fase 3)
    BIZ, activar, enviar, equipo, hora_fija, modelos, negocio, texto,
)


@pytest.fixture
def horario(db, negocio):
    for dia in ["mon", "tue", "wed", "thu", "fri"]:
        db.sembrar("business_hours", {"business_id": BIZ, "day": dia, "is_open": True, "opening_time": "09:00:00",
                                      "closing_time": "18:30:00", "lunch_start": "12:00:00", "lunch_end": "13:00:00"})
    db.sembrar("business_hours", {"business_id": BIZ, "day": "sat", "is_open": True, "opening_time": "10:00:00",
                                  "closing_time": "14:00:00", "lunch_start": None, "lunch_end": None})
    db.sembrar("business_hours", {"business_id": BIZ, "day": "sun", "is_open": False, "opening_time": None,
                                  "closing_time": None, "lunch_start": None, "lunch_end": None})


# --- detectores -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mensaje", ["¿Cuál es el horario?", "a qué hora abren", "¿Hasta qué hora atienden?",
                                     "que dias abren", "abren los domingos?", "horarios de atención"])
def test_preguntas_por_horario(mensaje):
    assert flujos.pregunta_por_horario(mensaje)


@pytest.mark.parametrize("mensaje", ["", None, "quiero agendar, ¿cuál es el horario?", "horario para mañana",
                                     "¿hay horario disponible el sábado?", "tienen cupo a las 3", "hola", "cuanto cuesta"])
def test_no_son_preguntas_de_horario(mensaje):
    assert not flujos.pregunta_por_horario(mensaje)


def test_hora_en_12_horas():
    assert flujos.hora_12h("09:00:00") == "9:00 a. m."
    assert flujos.hora_12h("18:30") == "6:30 p. m."
    assert flujos.hora_12h("12:00:00") == "12:00 p. m."
    assert flujos.hora_12h("00:15:00") == "12:15 a. m."


def test_faq_es_un_flujo_disponible():
    assert "faq" in flujos.FLUJOS_DISPONIBLES and flujos.flujos_activos("faq") == {"faq"}


# --- horario sin modelo -----------------------------------------------------------------------------------------

def test_el_horario_se_responde_sin_modelo_en_12_horas(horario, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    respuesta, _ = enviar("¿Cuál es el horario?")

    assert "Lunes: 9:00 a. m. a 6:30 p. m. (almuerzo de 12:00 p. m. a 1:00 p. m.)" in respuesta
    assert "Sábado: 10:00 a. m. a 2:00 p. m." in respuesta and "Domingo: cerrado" in respuesta
    assert "18:30" not in respuesta
    assert modelos.principal.llamadas == [] and modelos.forzado.llamadas == [] and modelos.simple.llamadas == []


def test_el_horario_responde_a_mitad_de_conversacion_y_la_conversacion_sigue(horario, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    sid = str(uuid.uuid4())
    modelos.principal.respuestas = [texto("¿Qué servicio quieres?")]
    enviar("quiero una cita", sid)
    assert "horario de atención" in enviar("¿a qué hora cierran?", sid)[0]
    assert len(modelos.principal.llamadas) == 1  # solo la primera

    modelos.principal.respuestas = [texto("Perfecto, ¿para qué día?")]
    assert enviar("el corte de cabello", sid)[0] == "Perfecto, ¿para qué día?"


def test_sin_horario_configurado_lo_contesta_el_modelo(negocio, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    modelos.principal.respuestas = [texto("Aún no tengo el horario cargado.")]
    assert enviar("¿cuál es el horario?")[0] == "Aún no tengo el horario cargado."


def test_negocio_sin_dias_abiertos_lo_contesta_el_modelo(db, negocio, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    db.sembrar("business_hours", {"business_id": BIZ, "day": "mon", "is_open": False, "opening_time": None,
                                  "closing_time": None, "lunch_start": None, "lunch_end": None})
    modelos.principal.respuestas = [texto("Por ahora no tengo días de atención.")]
    assert enviar("horario")[0] == "Por ahora no tengo días de atención."


def test_horario_de_un_dia_concreto_es_disponibilidad_y_va_al_modelo(horario, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    modelos.principal.respuestas = [texto("Déjame revisar.")]
    assert enviar("¿qué horario hay para mañana?")[0] == "Déjame revisar."


# --- servicios sin modelo ---------------------------------------------------------------------------------------

def test_los_servicios_se_responden_sin_modelo_con_botones(negocio, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    sid = str(uuid.uuid4())
    respuesta, opciones = enviar("¿Cuánto cuesta un corte?", sid)

    assert "*Corte de cabello* - 30 min - $15.000" in respuesta and "*Corte y barba* - 45 min - $22.000" in respuesta
    assert "Bienvenido" not in respuesta  # no es un saludo
    assert [o["value"] for o in opciones] == ["Corte de cabello", "Corte y barba"]
    assert modelos.principal.llamadas == [] and modelos.forzado.llamadas == []

    estado = g.GRAPH.get_state(g._thread_config(BIZ, sid)).values
    assert estado["employee_id"] == "emp-1" and estado["messages"][2].name == "consultar_servicios_disponibles"


def test_los_servicios_responden_aunque_ya_haya_conversacion(negocio, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    sid = str(uuid.uuid4())
    modelos.principal.respuestas = [texto("Hola, ¿en qué te ayudo?")]
    enviar("necesito ayuda con algo", sid)
    assert "*Corte y barba*" in enviar("¿qué servicios tienen?", sid)[0]
    assert len(modelos.principal.llamadas) == 1


def test_con_varios_empleados_los_servicios_los_decide_el_modelo(negocio, equipo, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    modelos.principal.respuestas = [texto("¿Con quién prefieres la cita?")]
    assert enviar("¿qué servicios tienen?")[0] == "¿Con quién prefieres la cita?"


def test_pedir_agendar_junto_con_el_precio_lo_decide_el_modelo(negocio, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    modelos.principal.respuestas = [texto("Claro, ¿cuál corte?")]
    assert enviar("quiero agendar un corte, ¿cuánto cuesta?")[0] == "Claro, ¿cuál corte?"


def test_con_la_bandera_apagada_todo_lo_hace_el_modelo(horario, modelos, monkeypatch):
    activar(monkeypatch, "")
    modelos.principal.respuestas = [texto("respuesta del modelo")]
    assert enviar("¿cuál es el horario?")[0] == "respuesta del modelo"
    assert len(modelos.principal.llamadas) == 1


def test_negocio_inexistente_no_toma_el_atajo_de_horario(db, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    assert g._tipo_faq({"business_id": "no-existe", "messages": [g.HumanMessage(content="horario")]}) is None


def test_una_conversacion_transferida_no_toma_atajos(negocio, modelos, monkeypatch):
    activar(monkeypatch, "faq")
    estado = {"business_id": BIZ, "messages": [g.HumanMessage(content="horario")], "transferido": True}
    assert g._tipo_faq(estado) is None
    assert g._tipo_faq({**estado, "transferido": False, "messages": []}) is None
    assert g._tipo_faq({**estado, "transferido": False, "messages": [g.AIMessage(content="hola")]}) is None


# --- medicion ---------------------------------------------------------------------------------------------------

def test_cada_respuesta_sin_modelo_deja_una_fila_de_cero_tokens(horario, modelos, monkeypatch, db):
    activar(monkeypatch, "faq,saludo")
    enviar("hola")
    enviar("¿cuál es el horario?")
    enviar("¿qué servicios tienen?")
    filas = [f for f in db.tablas["ai_usage_log"] if str(f.get("call_type")).startswith("sin_modelo")]
    assert sorted(f["call_type"] for f in filas) == ["sin_modelo_faq_horario", "sin_modelo_faq_servicios", "sin_modelo_saludo"]
    assert all(f["total_tokens"] == 0 and f["estimated_cost_usd"] == 0 and f["model"] == "ninguno" for f in filas)
