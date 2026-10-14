"""Optimizacion de tokens, Fase 0: salvaguardas de costo del chat publico y medicion de tokens."""

import uuid
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from routes import chat_routes
from services import ai_usage_tracking as uso, metrics, rate_limit
from tests.fakes import ModeloGuionado

BIZ = "biz-1"


@pytest.fixture
def negocio(db):
    db.sembrar("businesses", {"id": BIZ, "name": "Barberia", "business_type": "Barberia", "owner_id": "dueno", "plan": "pro",
                              "blocked": False, "phone_number": "3001234567"})
    db.sembrar("employees", {"id": "emp-1", "business_id": BIZ, "user_id": "dueno", "name": "Chenier", "role": "owner", "active": True})


@pytest.fixture
def modelo(monkeypatch):
    from agent import graph

    guion = ModeloGuionado()
    monkeypatch.setattr(graph, "_model", guion)
    monkeypatch.setattr(graph, "_model_forzar_servicios", ModeloGuionado())
    return guion


def _enviar(cliente, mensaje="hola", sid=None, ruta=None, headers=None):
    sid = sid or str(uuid.uuid4())
    ruta = ruta or f"/chat/{BIZ}/sessions/{sid}/messages"
    return cliente.post(ruta, json={"mensaje": mensaje}, headers=headers or {})


# --- limitador ---------------------------------------------------------------------------------------

def test_limitador_de_ventana(monkeypatch):
    reloj = [100.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: reloj[0])
    assert [rate_limit.permitir("k", 2, 60) for _ in range(3)] == [True, True, False]
    reloj[0] += 61  # la ventana avanza: vuelve a haber cupo
    assert rate_limit.permitir("k", 2, 60) is True
    assert rate_limit.permitir("otra", 2, 60) is True  # claves independientes
    assert rate_limit.permitir("sin-limite", 0, 60) is True and rate_limit.permitir("sin-limite", 0, 60) is True


def test_limitador_purga_claves_vencidas(monkeypatch):
    monkeypatch.setattr(rate_limit, "_MAX_CLAVES", 3)
    reloj = [0.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: reloj[0])
    for i in range(3):
        rate_limit.permitir(f"vieja-{i}", 5, 60)
    reloj[0] = 10_000.0
    rate_limit.permitir("nueva-1", 5, 60)
    rate_limit.permitir("nueva-2", 5, 60)  # supera el tope: se purgan las vencidas
    assert not any(k.startswith("vieja") for k in rate_limit._eventos)
    assert "nueva-2" in rate_limit._eventos


# --- chat publico: largo, ritmo y presupuesto ---------------------------------------------------------

def test_mensaje_demasiado_largo_no_llama_al_modelo(cliente, negocio, modelo, monkeypatch):
    monkeypatch.setattr(chat_routes, "CHAT_MENSAJE_MAX_CARACTERES", 20)
    r = _enviar(cliente, "x" * 21)
    assert r.status_code == 200 and "muy largo" in r.json()["respuesta"]
    assert modelo.llamadas == []

    modelo.respuestas = [AIMessage(content="ok")]
    assert _enviar(cliente, "x" * 20).json()["respuesta"] == "ok"  # en el limite, pasa


def test_limite_por_sesion_por_minuto(cliente, negocio, modelo, monkeypatch):
    monkeypatch.setattr(chat_routes, "CHAT_LIMITE_SESION_POR_MINUTO", 2)
    sid = str(uuid.uuid4())
    modelo.respuestas = [AIMessage(content="a"), AIMessage(content="b")]
    assert _enviar(cliente, sid=sid).status_code == 200
    assert _enviar(cliente, sid=sid).status_code == 200
    r = _enviar(cliente, sid=sid)
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    assert len(modelo.llamadas) == 2  # el tercero no costo tokens
    # Otra sesion sigue funcionando.
    modelo.respuestas = [AIMessage(content="c")]
    assert _enviar(cliente).status_code == 200


def test_limite_por_hora_de_una_sesion(cliente, negocio, modelo, monkeypatch):
    monkeypatch.setattr(chat_routes, "CHAT_LIMITE_SESION_POR_HORA", 1)
    sid = str(uuid.uuid4())
    modelo.respuestas = [AIMessage(content="a")]
    assert _enviar(cliente, sid=sid).status_code == 200
    assert _enviar(cliente, sid=sid).status_code == 429


def test_limite_por_ip_usa_la_ultima_entrada_de_x_forwarded_for(cliente, negocio, modelo, monkeypatch):
    monkeypatch.setattr(chat_routes, "CHAT_LIMITE_IP_POR_MINUTO", 1)
    modelo.respuestas = [AIMessage(content="a"), AIMessage(content="b")]
    # El cliente puede falsear las primeras entradas; la del proxy de confianza es la ultima.
    assert _enviar(cliente, headers={"x-forwarded-for": "9.9.9.9, 1.1.1.1"}).status_code == 200
    assert _enviar(cliente, headers={"x-forwarded-for": "8.8.8.8, 1.1.1.1"}).status_code == 429
    assert _enviar(cliente, headers={"x-forwarded-for": "8.8.8.8, 2.2.2.2"}).status_code == 200


def test_limite_por_ip_sin_encabezado_usa_la_ip_de_la_conexion(cliente, negocio, modelo, monkeypatch):
    monkeypatch.setattr(chat_routes, "CHAT_LIMITE_IP_POR_MINUTO", 1)
    modelo.respuestas = [AIMessage(content="a")]
    assert _enviar(cliente).status_code == 200
    assert _enviar(cliente).status_code == 429


def test_salvaguardas_tambien_en_el_chat_de_un_empleado(cliente, negocio, modelo, monkeypatch):
    monkeypatch.setattr(chat_routes, "CHAT_MENSAJE_MAX_CARACTERES", 5)
    sid = str(uuid.uuid4())
    r = cliente.post(f"/chat/{BIZ}/emp-1/sessions/{sid}/messages", json={"mensaje": "x" * 6})
    assert "muy largo" in r.json()["respuesta"] and modelo.llamadas == []


def test_corte_duro_de_presupuesto(cliente, negocio, modelo, db, monkeypatch):
    from core import settings

    db.sembrar("ai_usage_log", {"business_id": BIZ, "estimated_cost_usd": 6.0, "created_at": datetime.now().isoformat()})
    # Apagado por defecto: el bot responde aunque el gasto supere el presupuesto.
    modelo.respuestas = [AIMessage(content="hola")]
    assert _enviar(cliente).json()["respuesta"] == "hola"

    monkeypatch.setattr(settings, "AI_PRESUPUESTO_CORTE_DURO", True)
    uso.limpiar_cache_presupuesto()
    llamadas_antes = len(modelo.llamadas)
    r = _enviar(cliente).json()["respuesta"]
    assert "no está disponible" in r and "3001234567" in r
    assert len(modelo.llamadas) == llamadas_antes


def test_corte_duro_respeta_el_presupuesto_del_negocio_y_cachea(negocio, db, monkeypatch):
    from core import settings

    monkeypatch.setattr(settings, "AI_PRESUPUESTO_CORTE_DURO", True)
    db.sembrar("ai_usage_log", {"business_id": BIZ, "estimated_cost_usd": 6.0, "created_at": datetime.now().isoformat()})
    assert uso.presupuesto_agotado({"id": BIZ, "monthly_ai_budget_usd": 10}) is False
    consultas = len(db.consultas)
    assert uso.presupuesto_agotado({"id": BIZ, "monthly_ai_budget_usd": 10}) is False
    assert len(db.consultas) == consultas  # segunda vez: del cache, sin ir a la base
    uso.limpiar_cache_presupuesto()
    assert uso.presupuesto_agotado({"id": BIZ, "monthly_ai_budget_usd": 5}) is True


def test_corte_avisa_al_dueno_una_sola_vez_por_mes(negocio, db, monkeypatch):
    from core import settings
    from services import push_notifications

    monkeypatch.setattr(settings, "AI_PRESUPUESTO_CORTE_DURO", True)
    push = MagicMock()
    monkeypatch.setattr(uso, "enviar_notificacion_presupuesto_agotado", push)
    db.sembrar("ai_usage_log", {"business_id": BIZ, "estimated_cost_usd": 6.0, "created_at": datetime.now().isoformat()})
    negocio_con_token = {"id": BIZ, "fcm_token": "fcm-1", "monthly_ai_budget_usd": 5}

    assert uso.presupuesto_agotado(negocio_con_token) is True
    push.assert_called_once_with("fcm-1", "US$ 6.00", "US$ 5.00")
    assert db.filas("businesses")[0]["ai_budget_cutoff_sent_month"] == datetime.now().strftime("%Y-%m")

    # Con el cache vencido sigue agotado, pero ya no vuelve a avisar este mes.
    uso._cache_agotado.clear()
    assert uso.presupuesto_agotado(negocio_con_token) is True
    assert push.call_count == 1

    # Tras un reinicio (memoria vacia) la marca persistente evita repetirlo.
    uso.limpiar_cache_presupuesto()
    assert uso.presupuesto_agotado({**negocio_con_token, "ai_budget_cutoff_sent_month": datetime.now().strftime("%Y-%m")}) is True
    assert push.call_count == 1


def test_corte_no_falla_si_el_aviso_o_la_marca_fallan(negocio, db, monkeypatch, capsys):
    from core import settings

    monkeypatch.setattr(settings, "AI_PRESUPUESTO_CORTE_DURO", True)
    db.sembrar("ai_usage_log", {"business_id": BIZ, "estimated_cost_usd": 6.0, "created_at": datetime.now().isoformat()})

    def push_roto(*a):
        raise RuntimeError("fcm caido")

    monkeypatch.setattr(uso, "enviar_notificacion_presupuesto_agotado", push_roto)
    original = uso.supabase.table

    class TablaSinColumna:
        def __init__(self, tabla):
            self._t = original(tabla)

        def __getattr__(self, nombre):
            if nombre == "update":
                raise RuntimeError("column ai_budget_cutoff_sent_month does not exist")
            return getattr(self._t, nombre)

    monkeypatch.setattr(uso.supabase, "table", lambda nombre: TablaSinColumna(nombre) if nombre == "businesses" else original(nombre))
    assert uso.presupuesto_agotado({"id": BIZ, "fcm_token": "x", "monthly_ai_budget_usd": 5}) is True
    salida = capsys.readouterr().out
    assert "Error avisando del corte" in salida and "No se pudo guardar la marca" in salida


# --- precios y registro de tokens -------------------------------------------------------------------

def test_costo_depende_del_modelo_y_de_los_tokens_cacheados():
    assert uso.calcular_costo(1_000_000, 1_000_000, modelo="gpt-4.1-mini") == 2.0
    # 800k cacheados: 200k a 0.40 + 800k a 0.10 + 1M salida a 1.60
    assert uso.calcular_costo(1_000_000, 1_000_000, cached_tokens=800_000, modelo="gpt-4.1-mini") == pytest.approx(0.08 + 0.08 + 1.6)
    assert uso.calcular_costo(1_000_000, 0, modelo="gpt-4.1") == 2.0
    assert uso.calcular_costo(1_000_000, 0, modelo="gpt-4.1-nano") == 0.1
    # Los cacheados nunca superan la entrada (dato corrupto no da costos negativos).
    assert uso.calcular_costo(100, 0, cached_tokens=500, modelo="gpt-4.1-mini") >= 0


def test_modelo_sin_precio_usa_el_de_mini_y_avisa_una_vez(capsys):
    uso._modelos_sin_precio_avisados.clear()
    assert uso.calcular_costo(1_000_000, 0, modelo="modelo-nuevo") == 0.4
    uso.calcular_costo(1_000_000, 0, modelo="modelo-nuevo")
    assert capsys.readouterr().out.count("sin precio para el modelo") == 1


def test_registrar_uso_guarda_tokens_cacheados_tipo_y_conversacion(db):
    costo = uso.registrar_uso(BIZ, 1000, 100, 1100, cached_tokens=800, tipo="re_llamada_anti_alucinacion", session_id="s-1", modelo="gpt-4.1-mini")
    fila = db.filas("ai_usage_log")[0]
    assert fila["cached_tokens"] == 800 and fila["call_type"] == "re_llamada_anti_alucinacion"
    assert fila["session_id"] == "s-1" and fila["model"] == "gpt-4.1-mini"
    assert fila["estimated_cost_usd"] == costo == uso.calcular_costo(1000, 100, 800, "gpt-4.1-mini")


def test_registrar_uso_cae_al_formato_anterior_si_faltan_las_columnas(db, monkeypatch):
    """Antes de correr tokens_uso_ia.sql el insert con columnas nuevas falla: no se debe perder el registro."""
    from tests.fakes import _Consulta, _Resultado

    original = _Consulta.execute

    def sin_columnas_nuevas(self):
        if self._tabla == "ai_usage_log" and self._operacion == "insert":
            filas = self._payload if isinstance(self._payload, list) else [self._payload]
            if any("cached_tokens" in f for f in filas):
                raise RuntimeError("column cached_tokens does not exist")
        return original(self)

    monkeypatch.setattr(_Consulta, "execute", sin_columnas_nuevas)
    uso.registrar_uso(BIZ, 10, 5, 15, cached_tokens=3)
    fila = db.filas("ai_usage_log")[0]
    assert fila["prompt_tokens"] == 10 and "cached_tokens" not in fila


# --- metricas de tokens -------------------------------------------------------------------------------

def test_metricas_incluyen_tokens_cache_y_costo_por_conversacion(db, monkeypatch):
    monkeypatch.setattr(metrics, "ahora_local", lambda: datetime(2026, 10, 15, 12, 0, 0))
    metrics.limpiar_cache()
    db.sembrar("businesses", {"id": BIZ, "name": "B", "created_at": "2026-09-01T10:00:00+00:00"})
    db.sembrar("chat_sessions",
               {"business_id": BIZ, "session_id": "c1", "started_at": "2026-10-05T14:00:00+00:00"},
               {"business_id": BIZ, "session_id": "c2", "started_at": "2026-10-06T14:00:00+00:00"})
    db.sembrar("ai_usage_log",
               {"business_id": BIZ, "estimated_cost_usd": 0.02, "prompt_tokens": 1000, "cached_tokens": 600,
                "completion_tokens": 100, "call_type": "turno", "created_at": "2026-10-05T14:00:00+00:00"},
               {"business_id": BIZ, "estimated_cost_usd": 0.02, "prompt_tokens": 1000, "cached_tokens": 200,
                "completion_tokens": 100, "call_type": "re_llamada_anti_alucinacion", "created_at": "2026-10-06T14:00:00+00:00"},
               {"business_id": BIZ, "estimated_cost_usd": 0.01, "created_at": "2026-10-07T14:00:00+00:00"},  # fila anterior a la migracion
               # T4.1: respuestas sin modelo; no cuentan como llamadas ni mueven los promedios.
               {"business_id": BIZ, "estimated_cost_usd": 0, "prompt_tokens": 0, "call_type": "sin_modelo_faq_horario",
                "created_at": "2026-10-07T15:00:00+00:00"},
               {"business_id": BIZ, "estimated_cost_usd": 0, "prompt_tokens": 0, "call_type": "sin_modelo_saludo",
                "created_at": "2026-10-07T16:00:00+00:00"})
    t = metrics.metricas_negocio(BIZ, date(2026, 10, 1), date(2026, 10, 15))["ia"]["tokens"]
    assert t == {
        "llamadas": 3, "entrada": 2000, "cacheados": 800, "salida": 200, "porcentaje_cache": 40.0,
        "re_llamadas_anti_alucinacion": 1, "respuestas_sin_modelo": 2, "llamadas_por_conversacion": 1.5,
        "tokens_entrada_por_conversacion": 1000, "costo_por_conversacion_usd": 0.025,
    }


def test_metricas_de_tokens_sin_conversaciones(db, monkeypatch):
    monkeypatch.setattr(metrics, "ahora_local", lambda: datetime(2026, 10, 15, 12, 0, 0))
    metrics.limpiar_cache()
    db.sembrar("businesses", {"id": BIZ, "name": "B", "created_at": "2026-09-01T10:00:00+00:00"})
    t = metrics.metricas_negocio(BIZ, date(2026, 10, 1), date(2026, 10, 15))["ia"]["tokens"]
    assert t["llamadas"] == 0 and t["porcentaje_cache"] is None and t["costo_por_conversacion_usd"] is None


# --- configuracion --------------------------------------------------------------------------------------

def test_entero_env_ignora_valores_invalidos(monkeypatch):
    from core import settings

    monkeypatch.setenv("X_TEST", "abc")
    assert settings._entero_env("X_TEST", 7) == 7
    monkeypatch.setenv("X_TEST", "12")
    assert settings._entero_env("X_TEST", 7) == 12
    monkeypatch.delenv("X_TEST")
    assert settings._entero_env("X_TEST", 7) == 7
