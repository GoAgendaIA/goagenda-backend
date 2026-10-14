"""
Pruebas de agent/graph.py. Las de enviar_mensaje ejecutan el grafo REAL de
LangGraph (checkpointer en memoria + las tools reales contra el Supabase en
memoria); solo el modelo de lenguaje es un doble con respuestas guionadas.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from agent import graph as g
from tests.fakes import ModeloGuionado

BIZ = "biz-1"


def llamada(nombre, args=None, call_id=None):
    return AIMessage(content="", tool_calls=[{"name": nombre, "args": args or {}, "id": call_id or f"call-{uuid.uuid4().hex[:6]}", "type": "tool_call"}])


@pytest.fixture
def negocio(db):
    db.sembrar("businesses", {"id": BIZ, "name": "Barberia Elegant", "phone_number": "573001112233", "home_visits_enabled": False})
    db.sembrar("employees", {"id": "emp-1", "business_id": BIZ, "name": "Chenier", "active": True, "role": "owner"})
    servicios = [
        {"id": "s1", "business_id": BIZ, "name": "Corte de cabello", "duration_minutes": 30, "price": 15000.0, "active": True},
        {"id": "s2", "business_id": BIZ, "name": "Corte y cejas", "duration_minutes": 30, "price": 16000.0, "active": True},
    ]
    db.sembrar("services", *[dict(s) for s in servicios])
    db.sembrar("employee_services", *[{"employee_id": "emp-1", "service_id": s["id"], "services": dict(s)} for s in servicios])
    db.sembrar("business_hours", {"business_id": BIZ, "day": "sat", "is_open": True, "opening_time": "07:00:00",
                                  "closing_time": "19:00:00", "lunch_start": "12:30:00", "lunch_end": "13:00:00"})


@pytest.fixture
def modelo(monkeypatch):
    """Reemplaza el LLM normal y el forzado; devuelve una funcion para cargar el guion."""
    normal, forzado = ModeloGuionado(), ModeloGuionado()
    monkeypatch.setattr(g, "_model", normal)
    monkeypatch.setattr(g, "_model_forzar_servicios", forzado)
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    return SimpleNamespace(normal=normal, forzado=forzado)


def sesion():
    return str(uuid.uuid4())


# ------------------------------------------------------------------------------
# Helpers puros
# ------------------------------------------------------------------------------

@pytest.mark.parametrize("texto, valor", [("15.000", 15000), ("15,000", 15000), ("15000.0", 15000), ("1.500.000", 1500000), ("7.0", 7), ("abc", None)])
def test_a_entero(texto, valor):
    assert g._a_entero(texto) == valor


def test_precios_no_respaldados(db):
    db.sembrar("home_visit_zones", {"business_id": BIZ, "name": "Betulia", "fee": 5000, "active": True})
    catalogo = [{"price": 15000.0}, {"price": None}]
    herramientas = [ToolMessage(content="{'monto_abono_cents': 750000, 'total': '$41.000'}", tool_call_id="x")]
    texto = "Corte $15.000, recargo $5.000, total $20.000, abono $7.500, otro $41.000 y gratis $0"
    assert g._precios_no_respaldados(texto, herramientas, catalogo, BIZ) == []
    assert g._precios_no_respaldados("Barba $10.000 y $15.000", [], catalogo, BIZ) == [10000]
    assert g._precios_no_respaldados("sin precios", [], catalogo, BIZ) == []


def test_servicios_mencionados_y_consulta_en_el_turno():
    catalogo = [{"name": "Corte de cabello"}, {"name": "Cejas"}, {"name": None}]
    assert g._servicios_mencionados("Tenemos CORTE DE CABELLO y cejas", catalogo) == ["Corte de cabello", "Cejas"]
    consulta = ToolMessage(content="{}", name="consultar_servicios_disponibles", tool_call_id="c")
    assert g._consulto_servicios_en_este_turno([HumanMessage(content="hola"), consulta]) is True
    assert g._consulto_servicios_en_este_turno([consulta, HumanMessage(content="hola")]) is False
    assert g._consulto_servicios_en_este_turno([]) is False


def test_construir_horario_texto(negocio):
    texto = g._construir_horario_texto(BIZ)
    assert "sabado: 07:00 a 19:00 (almuerzo 12:30 a 13:00, no se agenda en ese rango)" in texto
    assert "lunes: cerrado" in texto and texto.count(";") == 6


def test_historial_valido_reordena_y_completa_tool_messages():
    ai = AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "t1"}, {"name": "b", "args": {}, "id": "t2"}])
    respuesta_t1 = ToolMessage(content="ok", tool_call_id="t1")
    historial = [HumanMessage(content="hola"), ai, HumanMessage(content="sigo"), respuesta_t1]
    resultado = g._historial_valido_para_modelo(historial)
    assert resultado[1] is ai and resultado[2] is respuesta_t1
    assert resultado[3].tool_call_id == "t2" and "problema tecnico" in resultado[3].content
    assert resultado[4].content == "sigo"


def test_invocar_modelo_reintenta_y_registra_uso(monkeypatch, negocio):
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    registrar = MagicMock()
    monkeypatch.setattr(g, "registrar_uso", registrar)
    respuesta = AIMessage(content="hola", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    modelo = ModeloGuionado(RuntimeError("timeout"), respuesta)
    assert g._invocar_modelo(modelo, [], BIZ) is respuesta
    registrar.assert_called_once_with(BIZ, 10, 5, 15, cached_tokens=0, tipo="turno", session_id=None)

    # Con tokens cacheados, el tipo de llamada y la conversacion se propagan al registro.
    registrar.reset_mock()
    cacheada = AIMessage(
        content="hola",
        usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105, "input_token_details": {"cache_read": 80}},
    )
    g._invocar_modelo(ModeloGuionado(cacheada), [], BIZ, tipo="re_llamada_anti_alucinacion", session_id="s-1")
    registrar.assert_called_once_with(
        BIZ, 100, 5, 105, cached_tokens=80, tipo="re_llamada_anti_alucinacion", session_id="s-1"
    )

    assert g._invocar_modelo(ModeloGuionado(*[RuntimeError("x")] * g.LLM_MAX_INTENTOS), [], BIZ) is None


# ------------------------------------------------------------------------------
# Botones de seleccion rapida
# ------------------------------------------------------------------------------

OPCIONES_HORAS = [{"label": "9:00 am", "value": "9:00 am"}, {"label": "9:30 am", "value": "9:30 am"}]


def tool_msg(nombre):
    return ToolMessage(content="{}", name=nombre, tool_call_id="c")


def test_opciones_coherentes():
    assert g._opciones_coherentes("pedir_confirmacion_cita", [{"value": "Si"}], "¿Confirmo tu cita?", []) is not None
    assert g._opciones_coherentes("pedir_confirmacion_cita", [{"value": "Si"}], "¿Tu nombre?", []) is None
    assert g._opciones_coherentes("consultar_horas_disponibles", OPCIONES_HORAS, "Tenemos 9:00 y 9:30", []) == OPCIONES_HORAS
    assert g._opciones_coherentes("consultar_horas_disponibles", OPCIONES_HORAS, "¿Para que dia?", []) is None
    servicios = [{"value": "Corte"}]
    assert g._opciones_coherentes("consultar_servicios_disponibles", servicios, "Tenemos Corte", ["quiero corte"]) is None
    assert g._opciones_coherentes("consultar_servicios_disponibles", servicios, "Tenemos Corte", ["hola"]) == servicios


def test_extraer_opciones():
    assert g._extraer_opciones([], OPCIONES_HORAS, None, None) is None
    assert g._extraer_opciones([tool_msg("crear_cita")], OPCIONES_HORAS, None, None) is None
    assert g._extraer_opciones([tool_msg("consultar_servicios_disponibles")], [{"value": "Corte"}], "s1", None) is None
    assert g._extraer_opciones([tool_msg("consultar_empleados_disponibles")], [{"value": "Ana"}], None, "e1") is None
    assert g._extraer_opciones([tool_msg("consultar_horas_disponibles")], None, None, None) is None
    assert g._extraer_opciones([tool_msg("consultar_horas_disponibles"), AIMessage(content="9:00")], OPCIONES_HORAS, None, None, "9:00 am") == OPCIONES_HORAS


# ------------------------------------------------------------------------------
# Nodo del agente (_agent_node)
# ------------------------------------------------------------------------------

def estado(**extra):
    return {"business_id": BIZ, "messages": [HumanMessage(content="Hola")], "employee_id": None, "employee_fijo": False, **extra}


def test_agent_node_transferido_y_negocio_inexistente(db):
    assert g._agent_node(estado(transferido=True))["messages"][0].content == g.MENSAJE_TRANSFERIDO
    assert g._agent_node(estado())["messages"][0].content == g.MENSAJE_ERROR_TECNICO


def test_agent_node_sin_respuesta_del_modelo_da_contacto_del_negocio(negocio, modelo):
    modelo.normal.respuestas = [RuntimeError("x")] * g.LLM_MAX_INTENTOS
    texto = g._agent_node(estado())["messages"][0].content
    assert texto.startswith(g.MENSAJE_ERROR_TECNICO) and "3001112233" in texto


def test_agent_node_incluye_catalogo_en_el_prompt(negocio, modelo):
    modelo.normal.respuestas = [AIMessage(content="¡Hola!")]
    g._agent_node(estado(employee_id="emp-1"))
    prompt = modelo.normal.llamadas[0][0].content
    # Solo nombres: con duracion y precio en el prompt el modelo dejaba de llamar la tool (sin botones).
    assert "- Corte de cabello\n- Corte y cejas\n" in prompt and "Corte de cabello - 30 min" not in prompt


def test_agent_node_fuerza_la_tool_si_inventa_precios(negocio, modelo):
    forzada = llamada("consultar_servicios_disponibles")
    modelo.normal.respuestas = [AIMessage(content="Barba $10.000 y Tratamiento $20.000")]
    modelo.forzado.respuestas = [forzada]
    assert g._agent_node(estado())["messages"][0] is forzada


def test_agent_node_fuerza_la_tool_si_lista_servicios_sin_consultar(negocio, modelo):
    forzada = llamada("consultar_servicios_disponibles")
    modelo.normal.respuestas = [AIMessage(content="Tenemos Corte de cabello y Corte y cejas")]
    modelo.forzado.respuestas = [forzada]
    assert g._agent_node(estado())["messages"][0] is forzada


def test_agent_node_no_fuerza_en_lista_de_empleados(negocio, modelo, db):
    db.sembrar("employees", {"id": "emp-2", "business_id": BIZ, "name": "Luis", "active": True})
    texto = "¿Con quién? Chenier hace Corte de cabello; Luis hace Corte y cejas"
    modelo.normal.respuestas = [AIMessage(content=texto)]
    assert g._agent_node(estado())["messages"][0].content == texto
    assert modelo.forzado.llamadas == []


def test_agent_node_si_la_llamada_forzada_falla_conserva_la_respuesta(negocio, modelo):
    original = AIMessage(content="Barba $10.000")
    modelo.normal.respuestas = [original]
    modelo.forzado.respuestas = [AIMessage(content="sin tool calls")]
    assert g._agent_node(estado())["messages"][0] is original


def test_agent_node_no_fuerza_dos_veces_en_el_mismo_turno(negocio, modelo):
    mensajes = [HumanMessage(content="Hola"), llamada("consultar_servicios_disponibles", call_id="c1"),
                ToolMessage(content="{}", name="consultar_servicios_disponibles", tool_call_id="c1")]
    modelo.normal.respuestas = [AIMessage(content="Barba $10.000")]
    g._agent_node(estado(messages=mensajes))
    assert modelo.forzado.llamadas == []


# ------------------------------------------------------------------------------
# enviar_mensaje: grafo real de punta a punta
# ------------------------------------------------------------------------------

def test_saludo_consulta_servicios_y_devuelve_botones(negocio, modelo):
    modelo.normal.respuestas = [
        llamada("consultar_servicios_disponibles"),
        AIMessage(content="¡Hola! Tenemos Corte de cabello y Corte y cejas. ¿Cuál quieres?"),
    ]
    texto, opciones = g.enviar_mensaje(BIZ, sesion(), "Hola")
    assert texto.startswith("¡Hola!")
    assert [o["value"] for o in opciones] == ["Corte de cabello", "Corte y cejas"]


def test_reintento_con_el_mismo_id_no_reprocesa(negocio, modelo):
    sid, mid = sesion(), str(uuid.uuid4())
    modelo.normal.respuestas = [AIMessage(content="Primera respuesta")]
    primera = g.enviar_mensaje(BIZ, sid, "Hola", client_message_id=mid)
    segunda = g.enviar_mensaje(BIZ, sid, "Hola", client_message_id=mid)
    assert primera == segunda == ("Primera respuesta", None)
    assert len(modelo.normal.llamadas) == 1
    historial, _ = g.obtener_historial(BIZ, sid)
    assert [m["role"] for m in historial] == ["user", "assistant"]


def test_reintento_de_un_mensaje_que_aun_no_tiene_respuesta(negocio, modelo):
    sid, mid = sesion(), str(uuid.uuid4())
    g.GRAPH.update_state(g._thread_config(BIZ, sid), {"messages": [HumanMessage(content="Hola", id=mid)], "business_id": BIZ})
    assert g.enviar_mensaje(BIZ, sid, "Hola", client_message_id=mid) == (g.MENSAJE_PROCESANDO, None)


def test_chat_de_empleado_fijo_guarda_el_empleado(negocio, modelo):
    sid = sesion()
    modelo.normal.respuestas = [AIMessage(content="Hola, soy el asistente de Chenier")]
    g.enviar_mensaje(BIZ, sid, "Hola", employee_id="emp-1")
    valores = g.GRAPH.get_state(g._thread_config(BIZ, sid)).values
    assert valores["employee_id"] == "emp-1" and valores["employee_fijo"] is True


def test_turno_sin_texto_final_da_error_tecnico(negocio, modelo):
    modelo.normal.respuestas = [AIMessage(content="")]
    assert g.enviar_mensaje(BIZ, sesion(), "Hola") == (g.MENSAJE_ERROR_TECNICO, None)


def test_limite_de_rondas(negocio, monkeypatch):
    monkeypatch.setattr(g.GRAPH, "invoke", MagicMock(side_effect=GraphRecursionError("limite")))
    texto, _ = g.enviar_mensaje(BIZ, sesion(), "Hola")
    assert texto.startswith(g.MENSAJE_LIMITE_RONDAS) and "3001112233" in texto


def test_limite_de_rondas_aunque_falle_la_base(negocio, monkeypatch):
    monkeypatch.setattr(g.GRAPH, "invoke", MagicMock(side_effect=GraphRecursionError("limite")))
    monkeypatch.setattr(g, "get_business_by_id", MagicMock(side_effect=RuntimeError("supabase caido")))
    assert g.enviar_mensaje(BIZ, sesion(), "Hola") == (g.MENSAJE_LIMITE_RONDAS, None)


def test_falla_de_la_base_al_leer_el_estado_no_lanza(negocio, monkeypatch, capsys):
    monkeypatch.setattr(g.GRAPH, "get_state", MagicMock(side_effect=RuntimeError("couldn't get a connection")))
    texto, opciones = g.enviar_mensaje(BIZ, sesion(), "Hola")
    assert texto.startswith(g.MENSAJE_ERROR_TECNICO) and opciones is None
    assert "Error inesperado en el chat" in capsys.readouterr().out


def test_mensaje_error_tecnico_sin_supabase(monkeypatch):
    monkeypatch.setattr(g, "get_business_by_id", MagicMock(side_effect=RuntimeError("caido")))
    assert g._mensaje_error_tecnico(BIZ) == g.MENSAJE_ERROR_TECNICO


# ------------------------------------------------------------------------------
# Mensajes escritos por fuera del modelo e historial
# ------------------------------------------------------------------------------

def test_respuesta_humana_detiene_al_bot(negocio, modelo):
    sid = sesion()
    g.enviar_respuesta_humana(BIZ, sid, "Hola, te atiende Chenier")
    assert g.enviar_mensaje(BIZ, sid, "gracias") == (g.MENSAJE_TRANSFERIDO, None)
    assert modelo.normal.llamadas == []


def test_notificacion_de_sistema(negocio, modelo):
    sid = sesion()
    g.enviar_notificacion_sistema(BIZ, sid, "Pago recibido")  # thread inexistente: no hace nada
    assert g.obtener_historial(BIZ, sid) == ([], None)

    modelo.normal.respuestas = [AIMessage(content="Hola")]
    g.enviar_mensaje(BIZ, sid, "Hola")
    g.enviar_notificacion_sistema(BIZ, sid, "✅ Pago recibido")
    historial, _ = g.obtener_historial(BIZ, sid)
    assert historial[-1] == {"role": "assistant", "content": "✅ Pago recibido"}
    assert not g.GRAPH.get_state(g._thread_config(BIZ, sid)).values.get("transferido")


def test_obtener_historial_restaura_los_botones_del_ultimo_turno(negocio, modelo):
    sid = sesion()
    modelo.normal.respuestas = [llamada("consultar_servicios_disponibles"), AIMessage(content="Tenemos Corte de cabello y Corte y cejas")]
    g.enviar_mensaje(BIZ, sid, "Hola")
    historial, opciones = g.obtener_historial(BIZ, sid)
    assert [m["role"] for m in historial] == ["user", "assistant"]
    assert [o["value"] for o in opciones] == ["Corte de cabello", "Corte y cejas"]


def test_obtener_historial_sin_botones_si_el_ultimo_es_del_cliente(negocio):
    sid = sesion()
    g.GRAPH.update_state(g._thread_config(BIZ, sid), {"messages": [HumanMessage(content="Hola")], "business_id": BIZ})
    assert g.obtener_historial(BIZ, sid) == ([{"role": "user", "content": "Hola"}], None)


def test_thread_aislado_por_negocio():
    assert g._thread_config("a", "s")["configurable"]["thread_id"] != g._thread_config("b", "s")["configurable"]["thread_id"]
