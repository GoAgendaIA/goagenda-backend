"""
Evaluacion EN VIVO del asistente contra el modelo real (opt-in; se omite por defecto).

    EVAL_LIVE=1 EVAL_OPENAI_API_KEY=sk-... EVAL_ETIQUETA=base pytest tests/test_evaluacion_en_vivo.py -s

Usa una clave propia de la evaluacion (no la de produccion) y solo puede alcanzar
api.openai.com. Ver evaluacion/README.md.
"""

import os

import pytest

from evaluacion import ejecutor
from evaluacion.escenarios import ESCENARIOS

EN_VIVO = os.environ.get("EVAL_LIVE") == "1"
pytestmark = pytest.mark.skipif(not EN_VIVO, reason="Evaluacion en vivo: definir EVAL_LIVE=1 y EVAL_OPENAI_API_KEY")

TOPE_USD = float(os.environ.get("EVAL_TOPE_USD") or 1.5)
MIN_ACIERTO = float(os.environ.get("EVAL_MIN_ACIERTO") or 0.85)


@pytest.fixture(scope="module")
def corrida():
    modelo = os.environ.get("EVAL_MODELO") or "gpt-4.1-mini"
    estado = {"medidor": ejecutor.Medidor(modelo), "resultados": []}
    yield estado

    from core import settings

    reporte = ejecutor.resumir(
        estado["resultados"],
        etiqueta=os.environ.get("EVAL_ETIQUETA") or "corrida",
        modelo=modelo,
        configuracion={"prompt": settings.CHAT_PROMPT_VERSION, "tools": settings.CHAT_TOOLS_VERSION,
                       "historial_turnos": settings.CHAT_HISTORIAL_MAX_TURNOS,
                       "flujos_deterministas": settings.CHAT_FLUJOS_DETERMINISTAS or "off",
                       "enrutamiento": settings.CHAT_ENRUTAMIENTO or "off",
                       "modelo_simple": settings.CHAT_MODEL_NAME_SIMPLE or None},
        tope_usd=TOPE_USD,
    )
    ruta = ejecutor.guardar_reporte(reporte)
    t = reporte["totales"]
    print(f"\\n=== Evaluacion: {t['aprobados']}/{t['ejecutados']} aprobados ({t['omitidos_por_tope']} omitidos por tope) | "
          f"{t['llamadas_modelo']} llamadas | {t['tokens_entrada']} tokens de entrada "
          f"({t['porcentaje_cache']}% cache) | US$ {t['costo_usd']} | reporte: {ruta}")


@pytest.mark.parametrize("escenario", ESCENARIOS, ids=lambda e: e.id)
def test_escenario(escenario, db, monkeypatch, corrida):
    from agent import graph

    if ejecutor.tope_alcanzado(corrida["medidor"], TOPE_USD):
        corrida["resultados"].append(ejecutor.omitido(escenario))
        pytest.skip(f"tope de gasto de US$ {TOPE_USD} alcanzado")

    ejecutor.preparar_entorno(monkeypatch)
    monkeypatch.setattr(graph, "registrar_uso", corrida["medidor"].registrar)
    resultado = ejecutor.ejecutar_escenario(escenario, db, graph.enviar_mensaje, corrida["medidor"])
    corrida["resultados"].append(resultado)

    assert resultado.error is None, resultado.error
    assert not resultado.fallos, "\\n".join(resultado.fallos) + "\\n--- respuestas ---\\n" + "\\n".join(resultado.respuestas)


def test_acierto_minimo(corrida):
    ejecutados = [r for r in corrida["resultados"] if not r.omitido]
    assert ejecutados, "no se ejecuto ningun escenario"
    acierto = sum(1 for r in ejecutados if r.aprobado) / len(ejecutados)
    assert acierto >= MIN_ACIERTO, f"acierto {acierto:.0%} < {MIN_ACIERTO:.0%}"
