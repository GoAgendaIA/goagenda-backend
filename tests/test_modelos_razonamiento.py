"""Parametros del cliente de OpenAI segun el modelo: los de razonamiento (gpt-6-luna) no llevan temperature."""

import pytest

from agent import graph as g
from agent.modelos import es_modelo_de_razonamiento, parametros_llm
from services import ai_usage_tracking as uso

BASE = dict(temperatura=0.3, max_tokens=600, max_tokens_razonamiento=4000)


@pytest.mark.parametrize("nombre", ["gpt-6-luna", "GPT-6-Luna", " gpt-6-luna-pro", "gpt-5-nano", "gpt-5.4-mini", "o1", "o3-mini", "o4-mini"])
def test_modelos_de_razonamiento(nombre):
    assert es_modelo_de_razonamiento(nombre)


@pytest.mark.parametrize("nombre", ["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o-mini", "claude-haiku", "", None])
def test_modelos_normales(nombre):
    assert not es_modelo_de_razonamiento(nombre)


def test_un_modelo_normal_conserva_temperature_y_su_tope_de_salida():
    assert parametros_llm("gpt-4.1", **BASE) == {"temperature": 0.3, "max_tokens": 600}
    # El esfuerzo de razonamiento no aplica a un modelo normal: enviarlo daria error.
    assert parametros_llm("gpt-4.1-mini", **BASE, esfuerzo_de_razonamiento="low") == {"temperature": 0.3, "max_tokens": 600}


def test_un_modelo_de_razonamiento_omite_temperature_y_amplia_el_tope():
    parametros = parametros_llm("gpt-6-luna", **BASE)
    assert "temperature" not in parametros
    assert parametros == {"max_tokens": 4000}


def test_el_esfuerzo_de_razonamiento_solo_se_envia_si_esta_configurado():
    assert parametros_llm("gpt-6-luna", **BASE, esfuerzo_de_razonamiento="low") == {"max_tokens": 4000, "reasoning_effort": "low"}
    assert "reasoning_effort" not in parametros_llm("gpt-6-luna", **BASE, esfuerzo_de_razonamiento=None)
    assert "reasoning_effort" not in parametros_llm("gpt-6-luna", **BASE, esfuerzo_de_razonamiento="")


def test_el_tope_nunca_baja_del_configurado_para_salida_normal():
    assert parametros_llm("gpt-6-luna", temperatura=0.3, max_tokens=9000, max_tokens_razonamiento=4000)["max_tokens"] == 9000


def test_el_cliente_real_no_manda_temperature_con_un_modelo_de_razonamiento():
    """ChatOpenAI arma el payload de la peticion: con gpt-6-luna no debe llevar temperature."""
    llm = g._crear_llm("gpt-6-luna")
    payload = llm._get_request_payload([("human", "hola")])
    assert payload["model"] == "gpt-6-luna"
    assert "temperature" not in payload
    assert payload["max_completion_tokens"] >= 4000

    normal = g._crear_llm("gpt-4.1")._get_request_payload([("human", "hola")])
    assert normal["temperature"] == 0.3 and normal["max_completion_tokens"] == 600


def test_el_esfuerzo_de_razonamiento_llega_al_payload(monkeypatch):
    monkeypatch.setattr(g, "LLM_REASONING_EFFORT", "low")
    payload = g._crear_llm("gpt-6-luna")._get_request_payload([("human", "hola")])
    assert payload.get("reasoning_effort") == "low"


def test_gpt_6_luna_tiene_precio_propio_y_no_cae_al_de_mini(capsys):
    assert uso.PRECIOS_POR_MILLON["gpt-6-luna"] == (0.10, 0.01, 0.50)
    # 1M de entrada (800k en cache) + 100k de salida.
    assert uso.calcular_costo(1_000_000, 100_000, 800_000, "gpt-6-luna") == pytest.approx(0.2 * 0.10 + 0.8 * 0.01 + 0.1 * 0.50, abs=1e-6)
    assert "sin precio" not in capsys.readouterr().out
