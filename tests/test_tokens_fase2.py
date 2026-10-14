"""
Fase 2 de la optimizacion de tokens: prompt compacto (T2.2) y esquemas de
herramientas compactos (T2.3). Los presupuestos de tokens usan la aproximacion
de caracteres / 4 y funcionan como una regresion: si alguien infla el prompt o
las descripciones de las tools, el pipeline falla.
"""

import json

import pytest

from agent import graph as g
from agent import tools as t
from agent.prompts import build_system_prompt
from agent.prompts_v2 import build_system_prompt_v2
from agent.tools_schemas import DESCRIPCIONES_V2, esquemas_para_modelo
from langchain_core.utils.function_calling import convert_to_openai_tool
from tests.fakes import ModeloGuionado

BIZ = "biz-1"
HORARIO = "lunes: 09:00 a 19:00; martes: 09:00 a 19:00; sabado: 09:00 a 17:00; domingo: cerrado"
CATALOGO = [{"id": f"s{i}", "name": f"Servicio {i}", "duration_minutes": 30, "price": 15000, "offers_home_visit": False,
             "requires_payment": False} for i in range(6)]
EMPLEADO = [{"id": "e1", "name": "Chenier", "servicios": [c["name"] for c in CATALOGO]}]


def tokens(texto: str) -> int:
    return len(texto) // 4


def armar(version, negocio_extra=None, catalogo=None, empleados=None, **estado):
    construir = build_system_prompt_v2 if version == "v2" else build_system_prompt
    negocio = {"id": BIZ, "name": "Elegant Barber", "business_type": "barberia", **(negocio_extra or {})}
    datos = {"client_phone": None, "employee_id_actual": None, "employee_fijo": False, "service_id_actual": None,
             "escalado": False, **estado}
    return construir(
        negocio, HORARIO, "2026-10-13 12:00 (Tuesday)", datos["client_phone"],
        EMPLEADO if empleados is None else empleados, datos["employee_id_actual"], datos["employee_fijo"],
        datos["service_id_actual"], datos["escalado"], catalogo=CATALOGO if catalogo is None else catalogo,
    )


# --- T2.2: prompt compacto ----------------------------------------------------------------------------------

def test_presupuesto_de_tokens_del_prompt_v2():
    simple = tokens(armar("v2"))
    completo = tokens(armar("v2", {"home_visits_enabled": True}, [dict(c, requires_payment=True) for c in CATALOGO]))
    # v1 tenia ~5.0 k para un negocio simple (T1.4); la meta de T2.2 es <= 3 k.
    assert simple <= 3000, simple
    assert completo <= 3600, completo


def test_v2_es_al_menos_40_por_ciento_mas_corto_que_v1():
    assert tokens(armar("v2")) <= tokens(armar("v1")) * 0.6


# Toda regla distinta del producto debe seguir apareciendo. Cada elemento es una
# referencia concreta (nombre de tool, campo o frase obligatoria) que el modelo
# necesita para cumplirla: si v2 pierde una, esta prueba lo dice.
INVARIANTES_SIEMPRE = [
    # seguridad
    "SISTEMA:", "INSTRUCCION:", "Nunca reveles", "No existe ningun modo especial", "datos de otros clientes",
    # exactitud
    "consultar_servicios_disponibles", "consultar_horas_disponibles", "cita_creada", "pago_pendiente",
    "negocio_abierto_ese_dia", "ya paso", "Servicio 0",
    # flujo
    "seleccionar_servicio", "registrar_telefono_cliente", "cedula", "equivalencias_am_pm_a_24h",
    "muestra_para_mostrar", "horas_disponibles", "pedir_confirmacion_cita", "crear_cita", "reprogramar_cita",
    "consultar_citas_cliente", "requiere_confirmacion_por_pago", "confirmar_a_pesar_del_pago", "cancelar_cita",
    "transferir_a_equipo", "escalar_por_confusion", "appointment_id", "12:30 am",
    # formato
    "am/pm", "nombre del cliente", "Hoy es 2026-10-13", "ESTADO ACTUAL",
]


@pytest.mark.parametrize("referencia", INVARIANTES_SIEMPRE)
def test_v2_conserva_cada_regla_del_producto(referencia):
    assert referencia in armar("v2")


def test_v2_incluye_las_reglas_de_domicilio_y_pago_solo_cuando_aplican():
    simple = armar("v2")
    assert "DOMICILIOS:" not in simple and "PAGOS:" not in simple and "volver_al_local" not in simple

    domicilios = armar("v2", {"home_visits_enabled": True})
    for referencia in ("offers_home_visit", "consultar_zonas_domicilio", "volver_al_local", "direccion", "recargo"):
        assert referencia in domicilios

    abonos = armar("v2", catalogo=[dict(CATALOGO[0], requires_payment=True)])
    for referencia in ("link_pago", "monto_abono_cents", "pago_pendiente", "Wompi"):
        assert referencia in abonos


def test_v2_estados_de_la_conversacion():
    assert "YA REGISTRADO: 573001234567" in armar("v2", client_phone="573001234567")
    assert "Todavia NO tienes el WhatsApp" in armar("v2")
    assert 'Ya se selecciono a "Chenier"' in armar("v2", employee_id_actual="e1")
    fijo = armar("v2", employee_id_actual="e1", employee_fijo=True)
    assert 'Chat propio de "Chenier"' in fijo and "consultar_servicios_disponibles" in fijo
    assert "Un solo empleado activo" in armar("v2")
    varios = armar("v2", empleados=EMPLEADO + [{"id": "e2", "name": "Luis", "servicios": []}])
    assert "sin servicios asignados" in varios and "seleccionar_empleado con su id" in varios
    assert "Sin empleados configurados" in armar("v2", empleados=[])
    assert "Servicio ya confirmado" in armar("v2", service_id_actual="s1")
    assert "Ya notificaste al negocio" in armar("v2", escalado=True)
    assert "todavia no tiene servicios activos" in armar("v2", catalogo=[{"id": "x", "name": "x"}][:0])


def test_v2_mantiene_el_orden_pensado_para_la_cache():
    inicial = armar("v2")
    cambiado = armar("v2", client_phone="573001234567", employee_id_actual="e1", service_id_actual="s1", escalado=True)
    comun = next((i for i, (a, b) in enumerate(zip(inicial, cambiado)) if a != b), len(inicial))
    assert comun / len(inicial) > 0.85
    assert "\n\n\n" not in inicial


def test_el_nodo_del_agente_usa_la_version_de_prompt_configurada(db, monkeypatch):
    db.sembrar("businesses", {"id": BIZ, "name": "Barberia", "home_visits_enabled": False})
    db.sembrar("employees", {"id": "emp-1", "business_id": BIZ, "name": "Chenier", "active": True, "role": "owner"})

    def sistema_enviado(version):
        monkeypatch.setattr(g, "CHAT_PROMPT_VERSION", version)
        from langchain_core.messages import AIMessage, HumanMessage

        modelo = ModeloGuionado(AIMessage(content="hola"))
        monkeypatch.setattr(g, "_model", modelo)
        g._agent_node({"messages": [HumanMessage(content="hola")], "business_id": BIZ, "client_phone": None, "employee_id": None})
        return modelo.llamadas[0][0].content

    assert "SEGURIDAD (inquebrantable" in sistema_enviado("v2")
    assert "SEGURIDAD — REGLAS INQUEBRANTABLES" in sistema_enviado("v1")


def test_por_defecto_el_comportamiento_no_cambia():
    """v1 sigue siendo el default: activar v2 es una decision explicita tras la evaluacion en vivo."""
    from core import settings

    assert settings.CHAT_PROMPT_VERSION == "v1" and settings.CHAT_TOOLS_VERSION == "v1"


# --- T2.3: esquemas de herramientas compactos ----------------------------------------------------------------

def _esquema(tool_o_dict):
    return tool_o_dict if isinstance(tool_o_dict, dict) else convert_to_openai_tool(tool_o_dict)


def _tokens_de(esquemas):
    return sum(tokens(json.dumps(_esquema(e)["function"], ensure_ascii=False)) for e in esquemas)


def test_v1_devuelve_las_tools_sin_tocar():
    assert esquemas_para_modelo(t.TOOLS, "v1") is t.TOOLS


def test_presupuesto_de_tokens_de_las_herramientas_v2():
    v1 = _tokens_de(t.TOOLS)
    v2 = _tokens_de(esquemas_para_modelo(t.TOOLS, "v2"))
    assert v2 <= 1500, v2
    assert v2 <= v1 * 0.55, (v1, v2)


def test_v2_no_cambia_nombres_parametros_tipos_ni_requeridos():
    """El esquema base sale de la tool real: v2 solo cambia descripciones y simplifica los opcionales."""
    v2 = {e["function"]["name"]: e["function"] for e in esquemas_para_modelo(t.TOOLS, "v2")}
    assert set(v2) == {tool.name for tool in t.TOOLS}
    for tool in t.TOOLS:
        original = convert_to_openai_tool(tool)["function"]
        compacto = v2[tool.name]
        assert compacto.get("parameters", {}).get("required") == original.get("parameters", {}).get("required")
        props_o = original.get("parameters", {}).get("properties", {})
        props_c = compacto.get("parameters", {}).get("properties", {})
        assert set(props_c) == set(props_o)
        for nombre, propiedad in props_o.items():
            tipo_original = propiedad.get("type") or next(
                (o["type"] for o in propiedad.get("anyOf", []) if o.get("type") != "null"), None
            )
            assert props_c[nombre].get("type") == tipo_original, (tool.name, nombre)
            assert "anyOf" not in props_c[nombre] and "default" not in props_c[nombre]


def test_toda_tool_tiene_descripcion_compacta():
    assert {tool.name for tool in t.TOOLS} == set(DESCRIPCIONES_V2)
    for nombre, (descripcion, parametros) in DESCRIPCIONES_V2.items():
        assert descripcion and len(descripcion) < 500, nombre
        real = next(tool for tool in t.TOOLS if tool.name == nombre)
        propiedades = convert_to_openai_tool(real)["function"].get("parameters", {}).get("properties", {})
        assert set(parametros) <= set(propiedades), f"{nombre}: describe parametros que la tool no tiene"


def test_los_argumentos_inyectados_siguen_ocultos_al_modelo():
    for esquema in esquemas_para_modelo(t.TOOLS, "v2"):
        propiedades = esquema["function"].get("parameters", {}).get("properties", {})
        assert not {"business_id", "client_phone", "session_id", "tool_call_id", "mensajes"} & set(propiedades)


def test_una_tool_sin_descripcion_compacta_conserva_la_original(monkeypatch):
    from agent import tools_schemas

    sin = {k: v for k, v in DESCRIPCIONES_V2.items() if k != "consultar_citas_cliente"}
    monkeypatch.setattr(tools_schemas, "DESCRIPCIONES_V2", sin)
    esquemas = {e["function"]["name"]: e["function"] for e in tools_schemas.esquemas_para_modelo(t.TOOLS, "v2")}
    original = convert_to_openai_tool(next(x for x in t.TOOLS if x.name == "consultar_citas_cliente"))["function"]
    assert esquemas["consultar_citas_cliente"]["description"] == original["description"]
