"""Estadisticas del negocio para su dueño: acceso, contenido (sin datos internos) y aviso de primera vez."""

from datetime import datetime

import pytest

from services import metrics
from tests.test_rutas_panel import BIZ, negocio  # noqa: F401

AHORA = datetime(2026, 10, 15, 12, 0, 0)
URL = f"/business-stats?business_id={BIZ}&desde=2026-10-01&hasta=2026-10-15"


@pytest.fixture(autouse=True)
def reloj(monkeypatch):
    monkeypatch.setattr(metrics, "ahora_local", lambda: AHORA)
    metrics.limpiar_cache()


@pytest.fixture
def datos(negocio, db):
    db.sembrar("services",
               {"id": "s1", "business_id": BIZ, "name": "Corte", "price": 15000, "active": True},
               {"id": "s2", "business_id": BIZ, "name": "Tinte", "price": 80000, "active": True})
    db.sembrar("business_hours", {"business_id": BIZ, "day": "mon", "is_open": True})
    base = {"business_id": BIZ, "employee_id": "emp-owner", "is_home_visit": False, "home_visit_fee": 0,
            "created_at": "2026-10-05T15:00:00+00:00"}
    db.sembrar("appointments",
               {**base, "client_phone": "3001111111", "service_id": "s1", "status": "confirmed", "source": "ai",
                "scheduled_at": "2026-10-06T10:00:00"},
               {**base, "client_phone": "3002222222", "service_id": "s2", "status": "confirmed", "source": "manual",
                "scheduled_at": "2026-10-20T10:00:00"},
               {**base, "client_phone": "3003333333", "service_id": "s1", "status": "cancelled", "source": "ai",
                "cancelled_by": "ai", "scheduled_at": "2026-10-08T10:00:00"})
    db.sembrar("chat_sessions", {"business_id": BIZ, "session_id": "c1", "started_at": "2026-10-05T14:00:00+00:00",
                                 "ended_in_appointment": True, "handed_off": False})
    # Datos internos de la plataforma que el dueño NO debe ver.
    db.sembrar("ai_usage_log", {"business_id": BIZ, "estimated_cost_usd": 0.42, "prompt_tokens": 1000, "completion_tokens": 50,
                                "cached_tokens": 600, "call_type": "turno", "created_at": "2026-10-05T14:00:00+00:00"})


def _claves(valor, acumulado=None):
    """Todas las claves anidadas, para comprobar que no se cuela ningun dato interno."""
    acumulado = set() if acumulado is None else acumulado
    if isinstance(valor, dict):
        for clave, hijo in valor.items():
            acumulado.add(clave)
            _claves(hijo, acumulado)
    elif isinstance(valor, list):
        for hijo in valor:
            _claves(hijo, acumulado)
    return acumulado


# --- acceso ---------------------------------------------------------------------------------------------------

def test_exige_sesion_y_solo_lo_ve_el_dueno(cliente, como, datos):
    assert cliente.get(URL).status_code == 401
    assert cliente.get(URL, headers=como("empleado")).status_code == 403   # un profesional no
    assert cliente.get(URL, headers=como("desconocido")).status_code == 403
    assert cliente.get(URL, headers=como("dueno")).status_code == 200


def test_el_dueno_de_otro_negocio_no_ve_estas_cifras(cliente, como, datos, db):
    db.sembrar("businesses", {"id": "b2", "name": "Otra", "owner_id": "otro", "blocked": False})
    assert cliente.get(URL, headers=como("otro")).status_code == 403


def test_negocio_inexistente_y_periodo_invalido(cliente, como, datos):
    assert cliente.get("/business-stats?business_id=fantasma", headers=como("dueno")).status_code == 404
    r = cliente.get(f"/business-stats?business_id={BIZ}&desde=2026-10-15&hasta=2026-10-01", headers=como("dueno"))
    assert r.status_code == 400


# --- contenido ---------------------------------------------------------------------------------------------------

def test_entrega_lo_util_para_decidir(cliente, como, datos):
    r = cliente.get(URL, headers=como("dueno")).json()

    assert r["tarjetas"]["citas_creadas"] == 3 and r["tarjetas"]["citas_ia"] == 2
    assert r["citas"]["por_estado"]["cancelled"] == 1
    assert r["rendimiento"]["ingresos_estimados"] == {
        "completadas": 15000.0, "por_realizar": 80000.0, "citas_por_realizar": 1, "recargos_domicilio": 0.0}
    # El ranking es del periodo (hasta el 15): el tinte del 20 es futuro y solo cuenta en "por realizar".
    assert [s["nombre"] for s in r["rendimiento"]["servicios_mas_agendados"]] == ["Corte"]
    assert r["asistente"]["conversaciones"]["iniciadas"] == 1 and r["asistente"]["conversaciones"]["con_cita"] == 1
    assert r["asistente"]["porcentaje_citas_ia"] is not None
    assert r["periodo"]["desde"] == "2026-10-01" and r["periodo"]["hasta"] == "2026-10-15"


def test_no_se_cuelan_datos_internos_de_la_plataforma(cliente, como, datos):
    claves = _claves(cliente.get(URL + "&comparar=true", headers=como("dueno")).json())
    prohibidas = {"costo_usd", "costo_por_cita_ia_usd", "costo_ia_usd", "tokens", "costo_por_conversacion_usd",
                  "tokens_entrada_por_conversacion", "entrada", "cacheados", "negocio", "criterios", "monthly_ai_budget_usd"}
    assert not (claves & prohibidas), claves & prohibidas


def test_la_comparacion_con_el_periodo_anterior(cliente, como, datos):
    r = cliente.get(URL + "&comparar=true", headers=como("dueno")).json()
    assert set(r["comparacion"]) == {"citas_creadas", "clientes_unicos", "citas_ia", "citas_manuales", "tasa_cancelacion", "conversion"}
    assert r["periodo"]["anterior"] is not None
    assert cliente.get(URL, headers=como("dueno")).json()["comparacion"] is None


def test_agrupacion_y_periodo_por_defecto(cliente, como, datos):
    r = cliente.get(f"/business-stats?business_id={BIZ}&group_by=week", headers=como("dueno")).json()
    assert r["periodo"]["agrupado_por"] == "week" and r["periodo"]["hasta"] == "2026-10-15"
    assert cliente.get(f"/business-stats?business_id={BIZ}&group_by=mal", headers=como("dueno")).status_code == 422


# --- aviso de primera vez ------------------------------------------------------------------------------------------

def test_me_informa_si_el_aviso_ya_se_vio(cliente, como, negocio, db):
    db.filas("employees")[0]["stats_intro_seen"] = False
    empleo = cliente.get("/me", headers=como("dueno")).json()["employments"][0]
    assert empleo["stats_intro_seen"] is False


def test_sin_la_columna_se_asume_visto_para_no_mostrar_un_aviso_que_no_se_recordaria(cliente, como, negocio):
    assert cliente.get("/me", headers=como("dueno")).json()["employments"][0]["stats_intro_seen"] is True


def test_marcar_el_aviso_como_visto(cliente, como, negocio, db):
    db.filas("employees")[0]["stats_intro_seen"] = False
    db.filas("employees")[1]["stats_intro_seen"] = False

    r = cliente.put("/business-settings/stats-intro-seen", json={"business_id": BIZ}, headers=como("dueno"))
    assert r.json() == {"stats_intro_seen": True, "guardado": True}
    por_id = {e["id"]: e["stats_intro_seen"] for e in db.filas("employees")}
    assert por_id == {"emp-owner": True, "emp-staff": False}   # solo el del dueño que lo vio
    assert cliente.get("/me", headers=como("dueno")).json()["employments"][0]["stats_intro_seen"] is True


def test_marcar_visto_exige_ser_el_dueno(cliente, como, negocio):
    cuerpo = {"business_id": BIZ}
    assert cliente.put("/business-settings/stats-intro-seen", json=cuerpo).status_code == 401
    assert cliente.put("/business-settings/stats-intro-seen", json=cuerpo, headers=como("empleado")).status_code == 403


def test_si_no_se_puede_guardar_no_rompe(cliente, como, negocio, db, capsys):
    db.fallar_en["employees"] = RuntimeError("columna inexistente")
    r = cliente.put("/business-settings/stats-intro-seen", json={"business_id": BIZ}, headers=como("dueno"))
    assert r.status_code == 200 and r.json()["guardado"] is False
    assert "No se pudo guardar" in capsys.readouterr().out
