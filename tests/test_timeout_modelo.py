"""Cada llamada al modelo tiene limite de tiempo: una llamada lenta de OpenAI no puede colgar el chat."""

from agent import graph as g
from core import settings


def test_el_cliente_de_openai_tiene_limite_de_tiempo():
    assert settings.LLM_TIMEOUT_SEGUNDOS == 25
    assert g._llm.request_timeout == settings.LLM_TIMEOUT_SEGUNDOS


def test_el_peor_caso_queda_por_debajo_del_corte_del_tunel():
    """3 intentos con su espera entre ellos deben terminar antes de los ~100 s de Cloudflare."""
    peor_caso = g.LLM_MAX_INTENTOS * settings.LLM_TIMEOUT_SEGUNDOS + (g.LLM_MAX_INTENTOS - 1) * g.LLM_ESPERA_ENTRE_INTENTOS_SEGUNDOS
    assert peor_caso < 90


def test_una_llamada_que_vence_el_tiempo_se_reintenta_y_luego_responde(monkeypatch):
    import openai

    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    intentos = []

    class ModeloLento:
        def invoke(self, entrada):
            intentos.append(1)
            if len(intentos) < 3:
                raise openai.APITimeoutError(request=None)
            return g.AIMessage(content="listo")

    respuesta = g._invocar_modelo(ModeloLento(), [], "biz-1")
    assert respuesta.content == "listo" and len(intentos) == 3


def test_si_todos_los_intentos_vencen_se_rinde_sin_colgarse(monkeypatch):
    import openai

    monkeypatch.setattr(g.time, "sleep", lambda s: None)

    class SiempreLento:
        def invoke(self, entrada):
            raise openai.APITimeoutError(request=None)

    assert g._invocar_modelo(SiempreLento(), [], "biz-1") is None
