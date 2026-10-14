"""El formato de las franjas Mañana/Tarde lo impone el servidor, no la redaccion del modelo."""

import json
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent import graph as g
from tests.test_fase3 import BIZ, enviar, hora_fija, llamada, modelos, negocio, texto  # noqa: F401

CANONICO = "🌅 *Mañana:* 9:00 am, 9:30 am\n🌇 *Tarde:* 2:00 pm, 3:30 pm"


def _horas(contenido=None):
    return ToolMessage(content=json.dumps({"horas_por_franja": CANONICO} if contenido is None else contenido),
                       name="consultar_horas_disponibles", tool_call_id="c1")


@pytest.mark.parametrize("reescrito", [
    "Mañana: 9:00 am, 9:30 am\nTarde: 2:00 pm, 3:30 pm",
    "🌅 Mañana: 9:00 am, 9:30 am\n🌇 Tarde: 2:00 pm, 3:30 pm",
    "**Mañana**: 9:00 am\n**Tarde**: 2:00 pm",
    "☀️ *Manana:* 9:00 am, 10:00 am\n🌙 *Tarde:* 5:00 pm",
])
def test_las_franjas_reescritas_se_reemplazan_por_las_de_la_herramienta(reescrito):
    texto_modelo = f"Estas son las horas para el viernes:\n{reescrito}\n¿Cuál prefieres?"
    assert g._con_franjas_canonicas(texto_modelo, [_horas()]) == (
        f"Estas son las horas para el viernes:\n{CANONICO}\n¿Cuál prefieres?"
    )


def test_sin_consulta_de_horas_el_texto_queda_igual():
    original = "Mañana: lo que sea\nTarde: otra cosa"
    assert g._con_franjas_canonicas(original, [AIMessage(content="hola")]) == original


def test_sin_lineas_de_franja_no_agrega_nada():
    assert g._con_franjas_canonicas("Ese día no atendemos 🙏", [_horas()]) == "Ese día no atendemos 🙏"


@pytest.mark.parametrize("contenido", [{"horas_por_franja": ""}, {"error": "x"}])
def test_sin_horas_libres_no_toca_el_texto(contenido):
    assert g._con_franjas_canonicas("Mañana: 9 am", [_horas(contenido)]) == "Mañana: 9 am"


def test_resultado_no_json_no_rompe():
    malo = ToolMessage(content="no es json", name="consultar_horas_disponibles", tool_call_id="c1")
    assert g._con_franjas_canonicas("Mañana: 9 am", [malo]) == "Mañana: 9 am"


def test_en_el_chat_real_el_cliente_recibe_las_franjas_en_negrita(negocio, modelos):
    modelos.principal.respuestas = [
        llamada("consultar_horas_disponibles", {"fecha": "2026-10-10"}),
        texto("Estas son las horas disponibles:\nMañana: 9:00 am, 9:30 am\nTarde: 2:00 pm\n¿Cuál prefieres?"),
    ]
    respuesta, _ = enviar("quiero para el sábado", str(uuid.uuid4()), employee_id="emp-1")
    assert "🌅 *Mañana:* 9:00 am, 9:30 am, 10:00 am" in respuesta
    assert "🌇 *Tarde:* " in respuesta and respuesta.rstrip().endswith("¿Cuál prefieres?")
    assert "Mañana: 9:00 am, 9:30 am\n" not in respuesta
