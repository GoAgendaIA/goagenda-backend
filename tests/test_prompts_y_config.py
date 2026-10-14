"""Variantes del system prompt segun el estado de la conversacion, y errores de arranque por configuracion faltante."""

import importlib

import pytest

from agent.prompts import build_system_prompt

NEGOCIO = {"name": "Barberia Elegant", "business_type": "Barberia"}
EMPLEADOS = [{"id": "e1", "name": "Chenier", "servicios": ["Corte"]}, {"id": "e2", "name": "Luis", "servicios": []}]


def prompt(**kw):
    base = dict(business=NEGOCIO, horario_texto="sabado: 07:00 a 19:00", fecha_actual="2026-10-07", client_phone=None,
                empleados=EMPLEADOS, employee_id_actual=None, employee_fijo=False)
    base.update(kw)
    return build_system_prompt(**base)


def test_identidad_y_horario():
    texto = prompt()
    assert texto.startswith('Eres el asistente virtual de "Barberia Elegant", un(a) Barberia')
    assert "sabado: 07:00 a 19:00" in texto
    assert "centro de estetica" in build_system_prompt({"name": "X"}, "", "", None, [], None, False)


def test_telefono_registrado_o_pendiente():
    assert "YA ESTA REGISTRADO: 573001234567" in prompt(client_phone="573001234567")
    assert "Todavia NO tienes el numero de WhatsApp" in prompt()


def test_estados_de_empleado():
    fijo = prompt(employee_id_actual="e1", employee_fijo=True)
    assert 'enlace propio de "Chenier"' in fijo and "Los unicos servicios validos son los suyos: Corte" in fijo
    assert "ninguno configurado todavia" in prompt(employee_id_actual="e2", employee_fijo=True)
    assert 'Ya se selecciono el empleado "Chenier"' in prompt(employee_id_actual="e1")
    unico = prompt(empleados=EMPLEADOS[:1])
    assert 'un solo empleado activo: "Chenier" (id e1)' in unico
    varios = prompt()
    assert "- Chenier (id e1): Corte" in varios and "- Luis (id e2): sin servicios asignados" in varios
    assert "todavia no tiene empleados configurados" in prompt(empleados=[])


def test_servicio_y_escalamiento():
    assert "Ya se confirmo con que servicio" in prompt(service_id_actual="s1")
    assert "Todavia no se ha confirmado con que servicio" in prompt()
    assert "Ya notificaste al negocio" in prompt(escalado=True)
    assert "Todavia no has notificado al negocio" in prompt()


def test_catalogo_solo_con_nombres():
    con = prompt(catalogo=[{"name": "Corte", "duration_minutes": 30, "price": 15000}])
    assert "- Corte\n" in con and "Corte - 30 min" not in con
    assert "todavia no tiene servicios activos" in prompt(catalogo=[])


def test_formato_precio_de_pagos():
    from services.wompi_payment_requests import _formato_precio_cop

    assert _formato_precio_cop(7500) == "$7.500" and _formato_precio_cop(None) == "$0" and _formato_precio_cop("x") == "$0"


# --- configuracion faltante al arrancar ---------------------------------------------

def _recargar_con(monkeypatch, modulo, variables_a_quitar):
    for variable in variables_a_quitar:
        monkeypatch.delenv(variable, raising=False)
    try:
        with pytest.raises(RuntimeError) as error:
            importlib.reload(modulo)
        return str(error.value)
    finally:
        monkeypatch.undo()
        importlib.reload(modulo)


def test_sin_database_url_no_arranca(monkeypatch):
    from core import settings

    assert "Falta DATABASE_URL" in _recargar_con(monkeypatch, settings, ["DATABASE_URL"])
    assert settings.DATABASE_URL  # quedo restaurado


def test_sin_credenciales_de_supabase_no_arranca(monkeypatch):
    from services import db

    assert "Faltan variables de Supabase" in _recargar_con(monkeypatch, db, ["SUPABASE_URL"])
    assert db.SUPABASE_URL == "http://supabase.test"


def test_pool_configurable_por_entorno(monkeypatch):
    from core import settings

    monkeypatch.setenv("DB_POOL_MAX_SIZE", "7")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "2.5")
    try:
        importlib.reload(settings)
        assert settings.DB_POOL_MAX_SIZE == 7 and settings.DB_POOL_TIMEOUT == 2.5 and settings.DB_POOL_MIN_SIZE == 1
    finally:
        monkeypatch.undo()
        importlib.reload(settings)
    assert settings.DB_POOL_MAX_SIZE == 4


# --- orden del prompt para la cache de OpenAI (T1.1) --------------------------------------------------------

def test_lo_que_cambia_en_cada_llamada_va_al_final_del_prompt():
    """
    OpenAI cachea el PREFIJO identico entre llamadas. Todo lo que cambia
    (fecha, telefono, empleado, servicio, escalamiento) debe ir despues de las
    reglas estables, o el descuento de tokens cacheados no se aplica.
    """
    base = dict(business={"id": "b", "name": "Barberia X", "business_type": "barberia"}, horario_texto="lunes: 09:00 a 19:00",
                empleados=[{"id": "e1", "name": "Chenier", "servicios": ["Corte"]}], employee_fijo=False,
                catalogo=[{"id": "s1", "name": "Corte", "duration_minutes": 30, "price": 15000}])

    def armar(**cambios):
        datos = {**base, "fecha_actual": "2026-10-13 10:00 (Tuesday)", "client_phone": None,
                 "employee_id_actual": None, "service_id_actual": None, "escalado": False, **cambios}
        return build_system_prompt(
            datos["business"], datos["horario_texto"], datos["fecha_actual"], datos["client_phone"], datos["empleados"],
            datos["employee_id_actual"], datos["employee_fijo"], datos["service_id_actual"], datos["escalado"], catalogo=datos["catalogo"],
        )

    inicial = armar()
    despues = armar(fecha_actual="2026-10-13 11:00 (Tuesday)", client_phone="573001234567", employee_id_actual="e1",
                    service_id_actual="s1", escalado=True)

    # El prefijo comun es casi todo el prompt (reglas estables), no unas decenas de tokens.
    comun = 0
    for a, b in zip(inicial, despues):
        if a != b:
            break
        comun += 1
    assert comun / len(inicial) > 0.9
    fin_de_reglas = inicial.index("REGLAS DE EXACTITUD")
    assert inicial.index("\nHoy es ") > fin_de_reglas
    assert inicial.rindex("ESTADO ACTUAL DE ESTA CONVERSACION") > fin_de_reglas
    # Todo lo que va antes de los datos volatiles es identico entre llamadas (prefijo cacheable).
    assert comun >= inicial.index("\nHoy es ")


# --- T1.4: secciones condicionales -------------------------------------------------------------------------

def _prompt_de(business_extra=None, catalogo=None):
    negocio = {"id": "b", "name": "Barberia X", "business_type": "barberia", **(business_extra or {})}
    return build_system_prompt(
        negocio, "lunes: 09:00 a 19:00", "2026-10-13 10:00 (Tuesday)", None,
        [{"id": "e1", "name": "Chenier", "servicios": ["Corte"]}], None, False, catalogo=catalogo or [{"name": "Corte"}],
    )


def test_negocio_simple_no_recibe_reglas_de_domicilios_ni_de_abonos():
    simple = _prompt_de()
    assert "DOMICILIOS:" not in simple and "REGLA CRITICA DE PAGOS" not in simple
    assert "cambie de local a domicilio" not in simple
    assert "\n\n\n" not in simple  # los bloques ausentes no dejan lineas vacias


def test_domicilios_activos_incluye_sus_reglas():
    prompt = _prompt_de({"home_visits_enabled": True})
    assert "DOMICILIOS:" in prompt and "cambie de local a domicilio" in prompt
    assert "REGLA CRITICA DE PAGOS" not in prompt


def test_servicio_con_abono_incluye_las_reglas_de_pago():
    prompt = _prompt_de(catalogo=[{"name": "Color", "requires_payment": True}])
    assert "REGLA CRITICA DE PAGOS" in prompt and "link_pago" in prompt
    assert "DOMICILIOS:" not in prompt


def test_las_secciones_condicionales_ahorran_tokens():
    simple = _prompt_de()
    completo = _prompt_de({"home_visits_enabled": True}, catalogo=[{"name": "Color", "requires_payment": True}])
    assert len(simple) < len(completo) * 0.9
    # La regla de cancelacion con pago es corta y se mantiene siempre (una cita ya pagada puede
    # existir aunque el servicio ya no este activo).
    assert "requiere_confirmacion_por_pago" in simple
