"""Centro de metricas: calculo (services/metrics.py), rutas de super admin y la instrumentacion que lo alimenta."""

from datetime import date, datetime

import pytest

from services import conversation_tracking, db as servicios_db, metrics

AHORA = datetime(2026, 10, 15, 12, 0, 0)
BIZ = "biz-1"
OTRO = "biz-2"
DESDE, HASTA = date(2026, 10, 1), date(2026, 10, 15)


@pytest.fixture(autouse=True)
def hora_fija(monkeypatch):
    monkeypatch.setattr(metrics, "ahora_local", lambda: AHORA)
    metrics.limpiar_cache()
    yield
    metrics.limpiar_cache()


def cita(business_id=BIZ, telefono="3001234567", servicio="s-corte", status="confirmed", source="ai",
         creada="2026-10-05T15:00:00+00:00", programada="2026-10-06T10:00:00", **extra):
    return {"business_id": business_id, "client_phone": telefono, "service_id": servicio, "employee_id": "emp-1",
            "status": status, "source": source, "created_at": creada, "scheduled_at": programada,
            "is_home_visit": False, "home_visit_fee": 0, **extra}


@pytest.fixture
def datos(db):
    db.sembrar("businesses",
               {"id": BIZ, "name": "Barberia Uno", "owner_id": "dueno", "created_at": "2026-09-01T10:00:00+00:00", "blocked": False},
               {"id": OTRO, "name": "Spa Dos", "owner_id": "otro", "created_at": "2026-08-01T10:00:00+00:00", "blocked": False})
    db.sembrar("services",
               {"id": "s-corte", "business_id": BIZ, "name": "Corte", "price": 15000, "active": True},
               {"id": "s-tinte", "business_id": BIZ, "name": "Tinte", "price": 80000, "active": True},
               {"id": "s-masaje", "business_id": OTRO, "name": "Masaje", "price": 50000, "active": True})
    db.sembrar("business_hours", {"business_id": BIZ, "day": "mon", "is_open": True},
               {"business_id": OTRO, "day": "mon", "is_open": True})
    db.sembrar("appointments",
               cita(),                                                                        # IA confirmada
               cita(telefono="+57 300 123 4567", status="completed", programada="2026-10-07T11:00:00"),  # mismo cliente, formato distinto
               cita(telefono="3109998888", source="manual", status="cancelled", cancelled_by=None,
                    programada="2026-10-08T09:00:00"),
               cita(telefono="3115550000", status="cancelled", cancelled_by="ai", programada="2026-10-08T10:00:00"),
               cita(telefono="3126660000", status="pending_payment", servicio="s-tinte", programada="2026-10-09T10:00:00"),
               cita(telefono="3137770000", status="cancelled", cancelled_by="system", servicio="s-tinte"),  # pago vencido
               cita(telefono="3148880000", source="unknown", rescheduled_count=2, rescheduled_by_ai_count=1,
                    is_home_visit=True, home_visit_fee=5000, status="completed", programada="2026-10-10T15:00:00"),
               cita(business_id=OTRO, telefono="3159990000", servicio="s-masaje", source="manual",
                    programada="2026-10-12T16:00:00"))
    db.sembrar("chat_sessions",
               {"business_id": BIZ, "session_id": "c1", "started_at": "2026-10-05T14:00:00+00:00", "ended_in_appointment": True, "handed_off": False},
               {"business_id": BIZ, "session_id": "c2", "started_at": "2026-10-06T14:00:00+00:00", "ended_in_appointment": False, "handed_off": True},
               {"business_id": BIZ, "session_id": "c3", "started_at": "2026-10-07T14:00:00+00:00", "ended_in_appointment": False, "handed_off": False},
               {"business_id": OTRO, "session_id": "c4", "started_at": "2026-10-07T14:00:00+00:00", "ended_in_appointment": False, "handed_off": False})
    db.sembrar("ai_usage_log",
               {"business_id": BIZ, "estimated_cost_usd": 0.02, "created_at": "2026-10-05T14:00:00+00:00"},
               {"business_id": BIZ, "estimated_cost_usd": 0.01, "created_at": "2026-10-06T14:00:00+00:00"},
               {"business_id": BIZ, "estimated_cost_usd": 9.99, "created_at": "2026-08-06T14:00:00+00:00"})


# --- calculo ------------------------------------------------------------------------------------------

def test_metricas_de_un_negocio(datos):
    r = metrics.metricas_negocio(BIZ, DESDE, HASTA)
    # El pago vencido (cancelled + system) no es una cita real.
    assert r["tarjetas"] == {"citas_creadas": 6, "clientes_unicos": 5, "citas_ia": 4, "citas_manuales": 1}
    assert r["citas"]["por_estado"] == {"pending": 0, "pending_payment": 1, "confirmed": 1, "completed": 2, "cancelled": 2}
    assert r["citas"]["pagos_vencidos"] == 1
    assert r["citas"]["tasa_cancelacion"] == 33.3 and r["citas"]["porcentaje_completadas"] == 33.3
    assert r["citas"]["reprogramadas"] == 1
    assert r["ia"]["por_origen"]["unknown"]["total"] == 1 and r["ia"]["por_origen"]["ai"]["porcentaje"] == 66.7
    assert r["ia"]["canceladas_por_ia"] == 1 and r["ia"]["reprogramadas_por_ia"] == 1
    assert r["ia"]["conversaciones"] == {"iniciadas": 3, "con_cita": 1, "transferidas_a_humano": 1, "conversion": 33.3}
    # El gasto de agosto queda fuera del periodo.
    assert r["ia"]["costo_usd"] == 0.03 and r["ia"]["costo_por_cita_ia_usd"] == 0.0075


def test_clientes_unicos_no_se_duplican_por_formato_de_telefono(datos):
    r = metrics.metricas_negocio(BIZ, DESDE, HASTA)
    assert r["clientes"]["total_registrados"] == 5
    assert r["clientes"]["nuevos_en_periodo"] == 5
    assert r["clientes"]["recurrentes"] == 1
    # Con 14 dias todos tuvieron cita hace menos que eso; con 3 dias, los 3 clientes con citas no
    # canceladas (la ultima fue entre el 7 y el 10 de octubre) ya llevan mas de 3 dias sin volver.
    assert r["clientes"]["sin_volver"] == {"dias": 14, "total": 0}
    r3 = metrics.metricas_negocio(BIZ, DESDE, HASTA, inactividad_dias=3)
    assert r3["clientes"]["sin_volver"] == {"dias": 3, "total": 3}


def test_evolucion_por_dia_semana_y_mes(datos):
    dia = metrics.metricas_negocio(BIZ, DESDE, HASTA)["citas"]["evolucion"]
    assert dia == [{"periodo": "2026-10-05", "total": 6, "ia": 4, "manual": 1}]
    assert metrics.metricas_negocio(BIZ, DESDE, HASTA, group_by="week")["citas"]["evolucion"][0]["periodo"] == "2026-10-05"
    assert metrics.metricas_negocio(BIZ, DESDE, HASTA, group_by="month")["citas"]["evolucion"][0]["periodo"] == "2026-10"


def test_la_fecha_se_agrupa_en_hora_de_colombia(db, datos):
    # 02:00 UTC del 6 de octubre = 21:00 del 5 de octubre en Colombia.
    db.sembrar("appointments", cita(creada="2026-10-06T02:00:00+00:00", telefono="3200000000"))
    evolucion = metrics.metricas_negocio(BIZ, DESDE, HASTA)["citas"]["evolucion"]
    assert evolucion[0]["periodo"] == "2026-10-05" and evolucion[0]["total"] == 7


def test_rendimiento_servicios_horas_domicilio_e_ingresos(datos):
    r = metrics.metricas_negocio(BIZ, DESDE, HASTA)["rendimiento"]
    servicios = {s["nombre"]: s["total"] for s in r["servicios_mas_agendados"]}
    assert servicios == {"Corte": 3, "Tinte": 1}
    assert r["domicilios"] == {"total": 1, "porcentaje": 25.0}
    assert {h["hora"] for h in r["citas_por_hora"]} == {10, 11, 15}
    assert sum(d["total"] for d in r["citas_por_dia_semana"]) == 4
    # Todas ya pasaron (AHORA es posterior): cuentan las no canceladas, aunque nadie las marcara "completed".
    assert r["ingresos_estimados"] == {"completadas": 125000.0, "por_realizar": 0.0, "citas_por_realizar": 0,
                                       "recargos_domicilio": 5000.0}


def test_ingresos_se_deciden_por_la_fecha_y_no_por_el_estado(db, datos):
    """Bug: las citas pasadas seguian "confirmed", asi que 'completadas' mostraba 0 y todo salia por realizar."""
    db.sembrar("appointments",
               cita(telefono="3170000001", programada="2026-10-14T09:00:00"),                      # pasada, confirmada
               cita(telefono="3170000002", programada="2026-10-14T10:00:00", status="cancelled", cancelled_by="ai"),
               cita(telefono="3170000003", programada="2026-10-20T10:00:00"),                      # futura, confirmada
               cita(telefono="3170000004", programada="2026-10-21T10:00:00", status="pending_payment", servicio="s-tinte"),
               cita(telefono="3170000005", programada="2026-10-22T10:00:00", status="cancelled", cancelled_by="ai"))
    ing = metrics.metricas_negocio(BIZ, DESDE, date(2026, 10, 31))["rendimiento"]["ingresos_estimados"]
    assert ing["completadas"] == 125000.0 + 15000.0       # las de antes + la pasada confirmada (la cancelada no suma)
    assert ing["por_realizar"] == 15000.0 + 80000.0       # solo las futuras no canceladas


def test_una_cita_de_hoy_cuya_hora_no_ha_llegado_sigue_por_realizar(db, datos):
    db.sembrar("appointments", cita(telefono="3180000001", programada=f"{AHORA.date().isoformat()}T23:30:00"))
    ing = metrics.metricas_negocio(BIZ, DESDE, date(2026, 10, 31))["rendimiento"]["ingresos_estimados"]
    assert ing["por_realizar"] == 15000.0


def test_global_es_la_suma_de_los_negocios(datos):
    g = metrics.metricas_globales(DESDE, HASTA)
    por_negocio = {n["business_id"]: n for n in g["negocios"]}
    assert g["tarjetas"]["citas_creadas"] == sum(n["citas_en_periodo"] for n in g["negocios"]) == 7
    assert g["tarjetas"]["citas_ia"] == sum(n["citas_ia_en_periodo"] for n in g["negocios"])
    assert por_negocio[BIZ]["citas_en_periodo"] == metrics.metricas_negocio(BIZ, DESDE, HASTA)["citas"]["total"]
    assert por_negocio[OTRO]["citas_manuales_en_periodo"] == 1
    # Cada negocio ve solo lo suyo.
    assert metrics.metricas_negocio(OTRO, DESDE, HASTA)["tarjetas"]["citas_creadas"] == 1


def test_adopcion_global(datos, db):
    db.sembrar("businesses", {"id": "biz-3", "name": "Nuevo", "created_at": "2026-10-14T10:00:00+00:00"})
    db.sembrar("businesses", {"id": "biz-4", "name": "Abandonado", "created_at": "2026-05-01T10:00:00+00:00"})
    db.sembrar("appointments", cita(business_id="biz-4", creada="2026-07-01T10:00:00+00:00", programada="2026-07-02T10:00:00"))
    # Solo agendamiento manual: activo, pero sin IA ni conversaciones.
    db.sembrar("businesses", {"id": "biz-5", "name": "Manual", "created_at": "2026-10-01T10:00:00+00:00"})
    db.sembrar("appointments", cita(business_id="biz-5", source="manual"))
    a = metrics.metricas_globales(DESDE, HASTA)["adopcion"]
    assert a["negocios_registrados"] == 5 and a["negocios_activos"] == 3
    assert a["negocios_inactivos"] == 1 and a["dejaron_de_usar"] == 1 and a["negocios_sin_uso"] == 1
    assert a["porcentaje_activos"] == 60.0
    assert a["con_horario"] == 2 and a["con_servicios"] == 2
    # OTRO solo tiene citas manuales pero si tuvo conversaciones con el asistente.
    assert a["usan_ia"] == 3 and a["solo_agendamiento_manual"] == 1
    assert a["dias_hasta_primera_cita"]["negocios_medidos"] == 4
    assert a["dias_hasta_primera_cita"]["mediana"] == 47.5


def test_comparacion_con_el_periodo_anterior(datos, db):
    db.sembrar("appointments", cita(creada="2026-09-20T15:00:00+00:00", telefono="3201111111"))
    r = metrics.metricas_negocio(BIZ, DESDE, HASTA, comparar=True)
    assert r["periodo"]["anterior"] == {"desde": "2026-09-16", "hasta": "2026-09-30"}
    assert r["comparacion"]["citas_creadas"] == {"actual": 6, "anterior": 1, "variacion": 5, "variacion_pct": 500.0}
    # Sin actividad el periodo anterior: la tasa no es calculable, no se inventa un cero.
    sin_datos = metrics.metricas_negocio(BIZ, date(2025, 1, 1), date(2025, 1, 31), comparar=True)
    assert sin_datos["comparacion"]["tasa_cancelacion"]["variacion"] is None


def test_negocio_sin_actividad_y_resultados_vacios(db):
    db.sembrar("businesses", {"id": "vacio", "name": "Sin nada", "created_at": "2026-10-01T10:00:00+00:00"})
    r = metrics.metricas_negocio("vacio", DESDE, HASTA)
    assert r["tarjetas"] == {"citas_creadas": 0, "clientes_unicos": 0, "citas_ia": 0, "citas_manuales": 0}
    assert r["citas"]["tasa_cancelacion"] is None and r["citas"]["evolucion"] == []
    assert r["ia"]["costo_por_cita_ia_usd"] is None and r["ia"]["conversaciones"]["conversion"] is None
    assert r["negocio"]["estado"] == "sin_uso" and r["negocio"]["ultima_actividad"] is None
    assert metrics.metricas_negocio("no-existe", DESDE, HASTA) is None


def test_plataforma_completamente_vacia(db):
    g = metrics.metricas_globales(DESDE, HASTA)
    assert g["negocios"] == [] and g["alertas"] == []
    assert g["adopcion"]["negocios_registrados"] == 0 and g["adopcion"]["porcentaje_activos"] is None
    assert g["adopcion"]["dias_hasta_primera_cita"]["mediana"] is None


def test_mediana_con_cantidad_par(db):
    for i, (reg, cita_dia) in enumerate([("2026-10-01", "2026-10-03"), ("2026-10-01", "2026-10-05")]):
        db.sembrar("businesses", {"id": f"b{i}", "name": f"N{i}", "created_at": f"{reg}T10:00:00+00:00"})
        db.sembrar("appointments", cita(business_id=f"b{i}", creada=f"{cita_dia}T15:00:00+00:00"))
    assert metrics.metricas_globales(DESDE, HASTA)["adopcion"]["dias_hasta_primera_cita"]["mediana"] == 3.0


def test_paginacion_no_trunca_en_1000_filas(db):
    db.sembrar("businesses", {"id": BIZ, "name": "Grande", "created_at": "2026-09-01T10:00:00+00:00"})
    db.sembrar("appointments", *[cita(telefono=f"31{i:08d}") for i in range(1250)])
    assert metrics.metricas_negocio(BIZ, DESDE, HASTA)["tarjetas"]["citas_creadas"] == 1250


def test_cache_y_refresco(datos, db):
    primero = metrics.metricas_negocio(BIZ, DESDE, HASTA)
    assert primero["cache"] is False
    db.sembrar("appointments", cita(telefono="3209999999"))
    assert metrics.metricas_negocio(BIZ, DESDE, HASTA)["cache"] is True
    assert metrics.metricas_negocio(BIZ, DESDE, HASTA)["tarjetas"]["citas_creadas"] == 6
    assert metrics.metricas_negocio(BIZ, DESDE, HASTA, refrescar=True)["tarjetas"]["citas_creadas"] == 7


def test_resolver_periodo(monkeypatch):
    assert metrics.resolver_periodo(None, None) == (date(2026, 9, 16), date(2026, 10, 15))
    assert metrics.resolver_periodo(date(2026, 10, 1), None) == (date(2026, 10, 1), date(2026, 10, 15))
    with pytest.raises(ValueError):
        metrics.resolver_periodo(date(2026, 10, 5), date(2026, 10, 1))
    with pytest.raises(ValueError):
        metrics.resolver_periodo(date(2024, 1, 1), date(2026, 10, 1))


def test_fechas_invalidas_no_rompen_el_calculo(db):
    db.sembrar("businesses", {"id": BIZ, "name": "X", "created_at": "no-es-fecha"})
    db.sembrar("appointments", cita(creada="basura", programada=None, telefono=None))
    r = metrics.metricas_negocio(BIZ, DESDE, HASTA)
    assert r["tarjetas"]["citas_creadas"] == 0 and r["negocio"]["dias_hasta_primera_cita"] is None


# --- alertas (fase 3) ---------------------------------------------------------------------------------

def _tipos(negocio_id):
    return {a["tipo"] for a in metrics.metricas_negocio(negocio_id, DESDE, HASTA, refrescar=True)["negocio"]["alertas"]}


def test_alerta_sin_configurar(db):
    db.sembrar("businesses", {"id": BIZ, "name": "X", "created_at": "2026-09-01T10:00:00+00:00"})
    assert _tipos(BIZ) == {"sin_configurar"}


def test_alerta_sin_primera_cita(db):
    db.sembrar("businesses", {"id": BIZ, "name": "X", "created_at": "2026-09-01T10:00:00+00:00"})
    db.sembrar("services", {"id": "s", "business_id": BIZ, "name": "Corte", "price": 1, "active": True})
    db.sembrar("business_hours", {"business_id": BIZ, "is_open": True})
    assert _tipos(BIZ) == {"sin_primera_cita"}


def test_alerta_inactivo(db):
    db.sembrar("businesses", {"id": BIZ, "name": "X", "created_at": "2026-05-01T10:00:00+00:00"})
    db.sembrar("services", {"id": "s", "business_id": BIZ, "name": "Corte", "price": 1, "active": True})
    db.sembrar("business_hours", {"business_id": BIZ, "is_open": True})
    db.sembrar("appointments", cita(creada="2026-07-01T10:00:00+00:00", programada="2026-07-02T10:00:00"))
    assert _tipos(BIZ) == {"inactivo"}


def test_alertas_de_cancelacion_conversion_solo_manual_y_costo(db):
    db.sembrar("businesses", {"id": BIZ, "name": "X", "created_at": "2026-10-01T10:00:00+00:00"})
    db.sembrar("services", {"id": "s-corte", "business_id": BIZ, "name": "Corte", "price": 1, "active": True})
    db.sembrar("business_hours", {"business_id": BIZ, "is_open": True})
    db.sembrar("appointments", *[
        cita(telefono=f"31{i:08d}", source="manual", status="cancelled" if i < 4 else "confirmed") for i in range(12)
    ])
    db.sembrar("chat_sessions", *[
        {"business_id": BIZ, "session_id": f"c{i}", "started_at": "2026-10-05T14:00:00+00:00"} for i in range(10)
    ])
    db.sembrar("ai_usage_log", {"business_id": BIZ, "estimated_cost_usd": 0.5, "created_at": "2026-10-05T14:00:00+00:00"})
    assert _tipos(BIZ) == {"cancelacion_alta", "conversion_baja", "solo_manual", "ia_sin_citas"}


def test_alertas_globales_ordenadas_y_sin_negocios_bloqueados(db):
    db.sembrar("businesses",
               {"id": "a", "name": "A", "created_at": "2026-09-01T10:00:00+00:00"},
               {"id": "b", "name": "B", "created_at": "2026-09-01T10:00:00+00:00", "blocked": True})
    alertas = metrics.metricas_globales(DESDE, HASTA)["alertas"]
    assert [a["business_id"] for a in alertas] == ["a"] and alertas[0]["recomendacion"]


# --- rutas (solo super admin) ---------------------------------------------------------------------------

def test_rutas_exigen_super_admin(cliente, como, datos, db):
    assert cliente.get("/admin/metrics/global").status_code == 401
    assert cliente.get("/admin/metrics/global", headers=como("dueno")).status_code == 403
    assert cliente.get(f"/admin/metrics/businesses/{BIZ}", headers=como("dueno")).status_code == 403

    db.sembrar("super_admins", {"user_id": "root"})
    r = cliente.get("/admin/metrics/global?desde=2026-10-01&hasta=2026-10-15&comparar=true", headers=como("root"))
    assert r.status_code == 200 and r.json()["tarjetas"]["citas_creadas"] == 7 and "comparacion" in r.json()

    r = cliente.get(f"/admin/metrics/businesses/{BIZ}?desde=2026-10-01&hasta=2026-10-15&group_by=week", headers=como("root"))
    assert r.status_code == 200 and r.json()["negocio"]["nombre"] == "Barberia Uno"
    assert r.json()["tarjetas"]["citas_creadas"] == 6


def test_rutas_validan_parametros(cliente, como, datos, db):
    db.sembrar("super_admins", {"user_id": "root"})
    h = como("root")
    assert cliente.get("/admin/metrics/global?desde=2026-10-10&hasta=2026-10-01", headers=h).status_code == 400
    assert cliente.get("/admin/metrics/global?desde=2020-01-01&hasta=2026-10-01", headers=h).status_code == 400
    assert cliente.get("/admin/metrics/global?group_by=year", headers=h).status_code == 422
    assert cliente.get("/admin/metrics/global?inactividad_dias=0", headers=h).status_code == 422
    assert cliente.get("/admin/metrics/businesses/no-existe", headers=h).status_code == 404
    assert cliente.get("/admin/metrics/businesses/no-existe?desde=2026-10-10&hasta=2026-10-01", headers=h).status_code == 400


# --- instrumentacion ----------------------------------------------------------------------------------------

def test_create_appointment_registra_el_origen(db):
    servicios_db.create_appointment(BIZ, "3001112233", "Ana", "s", "2026-10-20T10:00:00", "emp-1", source="ai")
    servicios_db.create_appointment(BIZ, "3001112233", "Ana", "s", "2026-10-21T10:00:00", "emp-1")
    assert [c["source"] for c in db.filas("appointments")] == ["ai", "manual"]


def test_cancelar_y_reprogramar_dejan_rastro(db):
    c = db.sembrar("appointments", {"business_id": BIZ, "client_phone": "3001112233", "status": "confirmed",
                                    "scheduled_at": "2026-10-20T10:00:00"})
    servicios_db.update_appointment_schedule(c["id"], "2026-10-21T10:00:00")
    servicios_db.update_appointment_schedule(c["id"], "2026-10-22T10:00:00")
    assert db.filas("appointments")[0]["rescheduled_by_ai_count"] == 2

    servicios_db.update_appointment_status(c["id"], "cancelled", actor="system")
    assert db.filas("appointments")[0]["cancelled_by"] == "system"
    servicios_db.update_appointment_status(c["id"], "confirmed", actor="system")
    assert db.filas("appointments")[0]["cancelled_by"] == "system"  # solo se anota al cancelar

    servicios_db.cancel_appointment(c["id"], "3001112233")
    assert db.filas("appointments")[0]["cancelled_by"] == "ai"


def test_registro_de_conversaciones(db):
    conversation_tracking.registrar_turno_conversacion(BIZ, "s1")
    conversation_tracking.registrar_turno_conversacion(BIZ, "s1")
    fila = db.filas("chat_sessions")[0]
    assert fila["message_count"] == 2 and not fila.get("ended_in_appointment")

    conversation_tracking.marcar_conversacion(BIZ, "s1", ended_in_appointment=True, appointment_id="c-1")
    assert db.filas("chat_sessions")[0]["ended_in_appointment"] is True

    # Si la fila no existia, el resultado no se pierde.
    conversation_tracking.marcar_conversacion(BIZ, "s2", handed_off=True)
    assert [f.get("handed_off") for f in db.filas("chat_sessions")] == [None, True]
    conversation_tracking.marcar_conversacion(BIZ, None, handed_off=True)
    assert len(db.filas("chat_sessions")) == 2


def test_la_analitica_nunca_tumba_la_conversacion(db):
    db.fallar_en["chat_sessions"] = RuntimeError("tabla sin migrar")
    conversation_tracking.registrar_turno_conversacion(BIZ, "s1")
    conversation_tracking.marcar_conversacion(BIZ, "s1", handed_off=True)


def test_por_realizar_cuenta_las_citas_futuras_aunque_el_periodo_termine_hoy(db, datos):
    """
    Bug devuelto por QA: con el periodo habitual (termina hoy) las citas futuras quedaban
    fuera del rango y "Por realizar" mostraba siempre $0.
    """
    db.sembrar("appointments",
               cita(telefono="3190000001", programada="2026-10-20T10:00:00"),                                  # Corte 15.000
               cita(telefono="3190000002", programada="2026-11-05T15:00:00", servicio="s-tinte"),              # Tinte 80.000
               cita(telefono="3190000003", programada="2026-10-22T10:00:00", status="pending_payment",
                    servicio="s-tinte"),                                                                         # 80.000
               cita(telefono="3190000004", programada="2026-10-21T10:00:00", status="cancelled", cancelled_by="ai"),
               cita(business_id=OTRO, telefono="3190000005", servicio="s-masaje", programada="2026-10-25T10:00:00"))
    ing = metrics.metricas_negocio(BIZ, DESDE, HASTA)["rendimiento"]["ingresos_estimados"]  # HASTA = 15 de octubre

    assert ing["por_realizar"] == 15000.0 + 80000.0 + 80000.0
    assert ing["citas_por_realizar"] == 3
    # Lo ya ocurrido sigue siendo del periodo: no se mezcla con lo futuro.
    assert ing["completadas"] == 125000.0


def test_por_realizar_no_depende_del_periodo_que_se_mire(db, datos):
    db.sembrar("appointments", cita(telefono="3190000006", programada="2026-10-20T10:00:00"))
    septiembre = metrics.metricas_negocio(BIZ, date(2026, 9, 1), date(2026, 9, 30))["rendimiento"]["ingresos_estimados"]
    octubre = metrics.metricas_negocio(BIZ, date(2026, 10, 1), date(2026, 10, 31))["rendimiento"]["ingresos_estimados"]
    assert septiembre["por_realizar"] == octubre["por_realizar"] == 15000.0
    assert septiembre["completadas"] == 0.0


def test_citas_con_fecha_invalida_no_cuentan_como_por_realizar(db, datos):
    db.sembrar("appointments", cita(telefono="3190000007", programada="no es fecha"))
    ing = metrics.metricas_negocio(BIZ, DESDE, HASTA)["rendimiento"]["ingresos_estimados"]
    assert ing["por_realizar"] == 0.0 and ing["citas_por_realizar"] == 0


# --- Costo promedio de IA por cita, en pesos (COP) -------------------------------------------------------------

def test_costo_por_cita_de_ia_en_pesos(datos, monkeypatch):
    """costo total de IA del periodo / citas agendadas por la IA, convertido con la tasa configurada."""
    monkeypatch.setattr(metrics, "COP_POR_USD", 4000.0)
    r = metrics.metricas_negocio(BIZ, DESDE, HASTA)
    ia, citas_ia = r["ia"], r["tarjetas"]["citas_ia"]

    assert citas_ia > 0 and ia["costo_usd"] > 0
    assert ia["tasa_cop_por_usd"] == 4000.0
    assert ia["costo_cop"] == round(ia["costo_usd"] * 4000)
    assert ia["costo_por_cita_ia_cop"] == round(ia["costo_usd"] * 4000 / citas_ia)
    # Coherente con el valor en dolares que ya existia.
    assert abs(ia["costo_por_cita_ia_cop"] - ia["costo_por_cita_ia_usd"] * 4000) <= 1


def test_la_tasa_configurada_cambia_el_resultado_y_se_informa(datos, monkeypatch):
    monkeypatch.setattr(metrics, "COP_POR_USD", 4000.0)
    base = metrics.metricas_negocio(BIZ, DESDE, HASTA, refrescar=True)["ia"]
    monkeypatch.setattr(metrics, "COP_POR_USD", 5000.0)
    otra = metrics.metricas_negocio(BIZ, DESDE, HASTA, refrescar=True)["ia"]

    assert otra["tasa_cop_por_usd"] == 5000.0
    assert otra["costo_por_cita_ia_cop"] > base["costo_por_cita_ia_cop"]
    assert abs(otra["costo_por_cita_ia_cop"] - base["costo_por_cita_ia_cop"] * 1.25) <= 2


def test_sin_citas_de_ia_no_hay_promedio(db, datos):
    db.reset()
    db.sembrar("businesses", {"id": BIZ, "name": "Solo manual", "owner_id": "d", "created_at": "2026-09-01T10:00:00+00:00", "blocked": False})
    db.sembrar("services", {"id": "s-corte", "business_id": BIZ, "name": "Corte", "price": 15000, "active": True})
    db.sembrar("appointments", cita(source="manual"))
    db.sembrar("ai_usage_log", {"business_id": BIZ, "estimated_cost_usd": 0.5, "created_at": "2026-10-05T14:00:00+00:00"})

    ia = metrics.metricas_negocio(BIZ, DESDE, HASTA)["ia"]
    assert ia["costo_por_cita_ia_cop"] is None and ia["costo_por_cita_ia_usd"] is None
    assert ia["costo_cop"] > 0  # el gasto existe aunque ninguna cita salio de la IA


def test_el_promedio_en_pesos_tambien_esta_en_la_comparacion_y_por_negocio(datos):
    comparado = metrics.metricas_negocio(BIZ, DESDE, HASTA, comparar=True)
    assert "costo_por_cita_ia_cop" in comparado["comparacion"]
    assert set(comparado["comparacion"]["costo_por_cita_ia_cop"]) == {"actual", "anterior", "variacion", "variacion_pct"}

    global_ = metrics.metricas_globales(DESDE, HASTA)
    fila = next(n for n in global_["negocios"] if n["business_id"] == BIZ)
    assert fila["costo_por_cita_ia_cop"] == metrics.metricas_negocio(BIZ, DESDE, HASTA)["ia"]["costo_por_cita_ia_cop"]


@pytest.mark.parametrize("valor, esperado", [("4150.5", 4150.5), ("", 4000.0), ("abc", 4000.0), ("0", 4000.0), ("-3", 4000.0)])
def test_la_tasa_se_lee_del_entorno_con_un_valor_seguro_por_defecto(monkeypatch, valor, esperado):
    from core import settings

    monkeypatch.setenv("COP_POR_USD", valor)
    assert settings._tasa_cop_por_usd() == esperado
