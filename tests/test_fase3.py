"""
Fase 3 de la optimizacion de tokens: atajos que evitan llamadas al modelo (T3.1) y
enrutamiento de modelo (T3.2). Corren el grafo REAL con modelos guionados: si un
atajo funciona, el modelo del paso no recibe ninguna llamada.
"""

import uuid
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent import flujos
from agent import graph as g
from agent import tools as t
from services import scheduling
from tests.fakes import ModeloGuionado

BIZ = "biz-1"
AHORA = datetime(2026, 10, 7, 8, 0)
SABADO = "2026-10-10"


def llamada(nombre, args=None, call_id=None):
    return AIMessage(content="", tool_calls=[{"name": nombre, "args": args or {}, "id": call_id or f"c-{uuid.uuid4().hex[:6]}",
                                              "type": "tool_call"}])


def texto(contenido):
    return AIMessage(content=contenido)


@pytest.fixture(autouse=True)
def hora_fija(monkeypatch):
    monkeypatch.setattr(scheduling, "ahora_local", lambda: AHORA)
    monkeypatch.setattr(t, "ahora_local", lambda: AHORA)
    monkeypatch.setattr(g, "ahora_local", lambda: AHORA)
    monkeypatch.setattr(t, "geocodificar", lambda direccion: None)


@pytest.fixture
def negocio(db):
    db.sembrar("businesses", {"id": BIZ, "name": "Barberia Elegant", "business_type": "barberia",
                              "phone_number": "573001112233", "home_visits_enabled": False})
    db.sembrar("employees", {"id": "emp-1", "business_id": BIZ, "name": "Chenier", "active": True, "role": "owner"})
    servicios = [
        {"id": "s1", "business_id": BIZ, "name": "Corte de cabello", "duration_minutes": 30, "price": 15000.0, "active": True},
        {"id": "s2", "business_id": BIZ, "name": "Corte y barba", "duration_minutes": 45, "price": 22000.0, "active": True},
    ]
    db.sembrar("services", *[dict(s) for s in servicios])
    db.sembrar("employee_services", *[{"employee_id": "emp-1", "service_id": s["id"], "services": dict(s)} for s in servicios])
    for dia in ["mon", "tue", "wed", "thu", "fri", "sat"]:
        db.sembrar("employee_hours", {"employee_id": "emp-1", "day": dia, "is_open": True, "opening_time": "09:00:00",
                                      "closing_time": "18:00:00", "lunch_start": None, "lunch_end": None})


@pytest.fixture
def equipo(db, negocio):
    db.sembrar("employees", {"id": "emp-2", "business_id": BIZ, "name": "Luis", "active": True, "role": "staff"})


@pytest.fixture
def modelos(monkeypatch):
    """Principal, forzado-a-servicios y simple, todos guionados y vacios."""
    principal, forzado, simple = ModeloGuionado(), ModeloGuionado(), ModeloGuionado()
    monkeypatch.setattr(g, "_model", principal)
    monkeypatch.setattr(g, "_model_forzar_servicios", forzado)
    monkeypatch.setattr(g, "_model_simple", simple)
    return SimpleNamespaceModelos(principal, forzado, simple)


class SimpleNamespaceModelos:
    def __init__(self, principal, forzado, simple):
        self.principal, self.forzado, self.simple = principal, forzado, simple


def activar(monkeypatch, flujos_="", enrutamiento="", simple_nombre=""):
    monkeypatch.setattr(g, "CHAT_FLUJOS_DETERMINISTAS", flujos_)
    monkeypatch.setattr(g, "CHAT_ENRUTAMIENTO", enrutamiento)
    monkeypatch.setattr(g, "CHAT_MODEL_NAME_SIMPLE", simple_nombre)


def enviar(texto_cliente, sid=None, **kw):
    return g.enviar_mensaje(BIZ, sid or str(uuid.uuid4()), texto_cliente, **kw)


# --- modulo flujos --------------------------------------------------------------------------------------------

def test_flujos_activos():
    assert flujos.flujos_activos("all") == frozenset(flujos.FLUJOS_DISPONIBLES)
    assert flujos.flujos_activos(" ON ") == frozenset(flujos.FLUJOS_DISPONIBLES)
    assert flujos.flujos_activos("") == frozenset() and flujos.flujos_activos(None) == frozenset()
    assert flujos.flujos_activos("saludo, servicios,inventado") == {"saludo", "servicios"}


@pytest.mark.parametrize("mensaje", ["hola", "Hola!", "¡Hola!", "buenas", "Buenas tardes", "buen día", "hola que tal",
                                     "HOLAA", "hey", "Buenos días 😊", "saludos", "hi"])
def test_saludos_puros(mensaje):
    assert flujos.es_saludo_puro(mensaje)


@pytest.mark.parametrize("mensaje", ["", None, "hola quiero un corte", "buenas, ¿cuánto cuesta?", "quiero agendar",
                                     "holanda", "hola hola hola mundo", "necesito ayuda"])
def test_no_son_saludos_puros(mensaje):
    assert not flujos.es_saludo_puro(mensaje)


@pytest.mark.parametrize("mensaje", ["¿Qué servicios tienen?", "cuanto cuesta el corte", "¿Cuáles son los precios?",
                                     "que hacen?", "me pasas el catálogo", "Cuánto vale un corte y barba"])
def test_preguntas_por_servicios(mensaje):
    assert flujos.pregunta_por_servicios(mensaje)


@pytest.mark.parametrize("mensaje", ["", None, "quiero agendar un corte, ¿cuánto cuesta?", "cancelar mi cita",
                                     "¿a qué hora abren?", "hola", "quiero hablar con alguien de los precios"])
def test_no_son_preguntas_de_servicios(mensaje):
    assert not flujos.pregunta_por_servicios(mensaje)


def test_emoji_segun_el_tipo_de_negocio():
    assert flujos.emoji_del_negocio("Barbería") == "💈"
    assert flujos.emoji_del_negocio("Spa y masajes") == "✨"
    assert flujos.emoji_del_negocio("Salón de uñas") == "💅"
    assert flujos.emoji_del_negocio(None) == "💇"


# --- T3.1: saludo -----------------------------------------------------------------------------------------------

def test_el_saludo_se_responde_sin_modelo(negocio, modelos, monkeypatch):
    activar(monkeypatch, "saludo")
    sid = str(uuid.uuid4())
    respuesta, opciones = enviar("¡Hola!", sid)

    assert "Bienvenido a Barberia Elegant" in respuesta
    assert "💈 *Corte de cabello* - 30 min - $15.000" in respuesta and "*Corte y barba* - 45 min - $22.000" in respuesta
    assert respuesta.rstrip().endswith("¿Cuál te interesa o quieres agendar una cita?")
    assert [o["value"] for o in opciones] == ["Corte de cabello", "Corte y barba"]
    assert modelos.principal.llamadas == [] and modelos.forzado.llamadas == [] and modelos.simple.llamadas == []

    # El historial queda como si el modelo hubiera consultado los servicios.
    estado = g.GRAPH.get_state(g._thread_config(BIZ, sid)).values
    tipos = [type(m).__name__ for m in estado["messages"]]
    assert tipos == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert estado["messages"][2].name == "consultar_servicios_disponibles" and estado["employee_id"] == "emp-1"

    # El siguiente turno ya lo atiende el modelo (con el historial completo).
    modelos.principal.respuestas = [texto("Perfecto, ¿para qué día?")]
    assert enviar("El corte de cabello", sid)[0] == "Perfecto, ¿para qué día?"
    assert len(modelos.principal.llamadas) == 1


def test_el_saludo_usa_el_modelo_si_la_bandera_esta_apagada(negocio, modelos, monkeypatch):
    activar(monkeypatch, "")
    modelos.principal.respuestas = [texto("Hola desde el modelo")]
    assert enviar("hola")[0] == "Hola desde el modelo" and len(modelos.principal.llamadas) == 1


def test_el_saludo_del_chat_de_un_empleado_presenta_el_enlace(negocio, modelos, monkeypatch):
    activar(monkeypatch, "saludo")
    respuesta, _ = enviar("buenas", employee_id="emp-1")
    assert "este enlace de chat pertenece a Chenier" in respuesta and modelos.principal.llamadas == []


@pytest.mark.parametrize("mensaje", ["hola quiero un corte", "necesito ayuda", "¿qué servicios tienen?"])
def test_lo_que_no_es_un_saludo_puro_lo_atiende_el_modelo(negocio, modelos, monkeypatch, mensaje):
    activar(monkeypatch, "saludo")
    modelos.principal.respuestas = [texto("respuesta del modelo")]
    assert enviar(mensaje)[0] == "respuesta del modelo"


def test_con_varios_empleados_el_saludo_lo_decide_el_modelo(negocio, equipo, modelos, monkeypatch):
    activar(monkeypatch, "saludo")
    modelos.principal.respuestas = [texto("¿Con quién prefieres la cita?")]
    assert enviar("hola")[0] == "¿Con quién prefieres la cita?"


def test_un_segundo_saludo_no_repite_la_lista(negocio, modelos, monkeypatch):
    activar(monkeypatch, "saludo")
    sid = str(uuid.uuid4())
    enviar("hola", sid)
    modelos.principal.respuestas = [texto("¡Hola de nuevo! ¿Cuál servicio te interesa?")]
    assert enviar("hola", sid)[0] == "¡Hola de nuevo! ¿Cuál servicio te interesa?"


def test_sin_servicios_o_sin_empleados_cae_al_modelo(db, modelos, monkeypatch):
    activar(monkeypatch, "saludo")
    db.sembrar("businesses", {"id": BIZ, "name": "Barberia Elegant", "home_visits_enabled": False})
    modelos.principal.respuestas = [texto("modelo 1"), texto("modelo 2")]
    assert enviar("hola")[0] == "modelo 1"  # sin empleados
    db.sembrar("employees", {"id": "emp-1", "business_id": BIZ, "name": "Chenier", "active": True, "role": "owner"})
    assert enviar("hola")[0] == "modelo 2"  # con empleado pero sin servicios


def test_negocio_inexistente_no_toma_el_atajo(db, modelos, monkeypatch):
    activar(monkeypatch, "saludo")
    assert g._datos_para_saludo({"business_id": "no-existe", "employee_id": None, "employee_fijo": False}) is None


def test_chat_de_un_empleado_inexistente_no_toma_el_atajo(negocio):
    estado = {"business_id": BIZ, "employee_fijo": True, "employee_id": "no-existe"}
    assert g._datos_para_saludo(estado) is None


def test_un_cliente_transferido_no_recibe_el_saludo(negocio, modelos, monkeypatch):
    activar(monkeypatch, "saludo")
    estado = {"messages": [HumanMessage(content="hola")], "business_id": BIZ, "transferido": True}
    assert g._ruta_inicial(estado) == "agent"
    assert g._ruta_inicial({**estado, "transferido": False, "escalado": True}) == "agent"
    assert g._ruta_inicial({"messages": [], "business_id": BIZ}) == "agent"
    assert g._ruta_inicial({"messages": [AIMessage(content="x")], "business_id": BIZ}) == "agent"


# --- T3.1: confirmacion directa ---------------------------------------------------------------------------------

MENSAJE_CLIENTE = "Corte de cabello el sábado, soy Ana y mi WhatsApp es 3001234567"


def _guion_confirmacion(modelos, hora="10:00:00"):
    modelos.principal.respuestas = [
        llamada("seleccionar_empleado", {"employee_id": "emp-1"}),
        llamada("registrar_telefono_cliente", {"numero_whatsapp": "3001234567"}),
        llamada("pedir_confirmacion_cita", {"servicio_nombre": "Corte de cabello", "fecha_hora": f"{SABADO}T{hora}",
                                            "nombre_cliente": "Ana"}),
        texto("Aqui el resumen reformulado por el modelo"),
    ]


def test_el_resumen_de_confirmacion_se_entrega_sin_otra_llamada(negocio, modelos, monkeypatch):
    activar(monkeypatch, "confirmacion")
    _guion_confirmacion(modelos)
    respuesta, opciones = enviar(MENSAJE_CLIENTE)
    assert "Confírmame estos datos" in respuesta and "*Corte de cabello*" in respuesta
    assert respuesta.rstrip().endswith("¿Confirmo tu cita?")
    assert [o["value"] for o in opciones] == ["Si", "No"]
    # seleccionar_empleado, registrar_telefono y pedir_confirmacion; sin la llamada final de relevo.
    assert len(modelos.principal.llamadas) == 3


def test_con_la_bandera_apagada_el_modelo_relata_el_resumen(negocio, modelos, monkeypatch):
    activar(monkeypatch, "")
    _guion_confirmacion(modelos)
    assert enviar(MENSAJE_CLIENTE)[0] == "Aqui el resumen reformulado por el modelo"
    assert len(modelos.principal.llamadas) == 4


def test_un_error_al_confirmar_lo_explica_el_modelo(negocio, modelos, monkeypatch):
    """Hora fuera de horario: el resultado de la tool es un error (sin nombre), no un resumen para entregar."""
    activar(monkeypatch, "confirmacion")
    _guion_confirmacion(modelos, hora="23:00:00")
    modelos.principal.respuestas[-1] = texto("Esa hora no está disponible 🙏 ¿otra hora?")
    assert enviar("Corte el sábado a las 11 de la noche, soy Ana, mi WhatsApp es 3001234567")[0] == (
        "Esa hora no está disponible 🙏 ¿otra hora?"
    )
    assert len(modelos.principal.llamadas) == 4


def test_resultados_recientes_y_destino_tras_las_herramientas():
    mensajes = [HumanMessage(content="a"), llamada("x", call_id="1"), ToolMessage(content="r1", name="x", tool_call_id="1"),
                ToolMessage(content="r2", name="pedir_confirmacion_cita", tool_call_id="2")]
    assert [m.content for m in g._resultados_recientes(mensajes)] == ["r1", "r2"]
    assert g._resultados_recientes([HumanMessage(content="a")]) == []


def test_un_error_con_el_nombre_de_la_herramienta_no_se_confunde_con_el_resumen(monkeypatch):
    """ToolNode le pone el nombre de la tool a sus mensajes de error: hay que distinguirlos por el contenido."""
    activar(monkeypatch, "confirmacion")
    error = ToolMessage(content="Falta el numero de WhatsApp del cliente. Pidelo.", name="pedir_confirmacion_cita", tool_call_id="1")
    resumen = ToolMessage(content="¡Perfecto! Confírmame estos datos 📋\n👤 Nombre: *Ana*\n¿Confirmo tu cita?",
                          name="pedir_confirmacion_cita", tool_call_id="2")
    assert g._despues_de_tools({"messages": [error]}) == "agent"
    assert g._despues_de_tools({"messages": [resumen]}) == "directo"
    assert not g._es_resumen_de_confirmacion(ToolMessage(content="¿Confirmo tu cita?", name="otra", tool_call_id="3"))


def test_con_varias_herramientas_a_la_vez_no_se_entrega_directo(monkeypatch):
    activar(monkeypatch, "confirmacion")
    una = [ToolMessage(content="Resumen\n¿Confirmo tu cita?", name="pedir_confirmacion_cita", tool_call_id="1")]
    dos = una + [ToolMessage(content="ok", name="registrar_telefono_cliente", tool_call_id="2")]
    assert g._despues_de_tools({"messages": una}) == "directo"
    assert g._despues_de_tools({"messages": dos}) == "agent"
    activar(monkeypatch, "")
    assert g._despues_de_tools({"messages": una}) == "agent"


# --- T3.1: servicios directo ----------------------------------------------------------------------------------------

def test_la_pregunta_por_servicios_hace_la_consulta_en_la_primera_llamada(negocio, modelos, monkeypatch):
    activar(monkeypatch, "servicios")
    modelos.forzado.respuestas = [llamada("consultar_servicios_disponibles")]
    modelos.principal.respuestas = [texto("Tenemos *Corte de cabello* por $15.000 y *Corte y barba* por $22.000")]
    respuesta, opciones = enviar("¿Qué servicios tienen y cuánto cuestan?")
    assert "Corte de cabello" in respuesta
    assert len(modelos.forzado.llamadas) == 1 and len(modelos.principal.llamadas) == 1
    assert [o["value"] for o in opciones] == ["Corte de cabello", "Corte y barba"]


def test_sin_la_bandera_la_primera_llamada_es_del_modelo_principal(negocio, modelos, monkeypatch):
    activar(monkeypatch, "")
    modelos.principal.respuestas = [llamada("consultar_servicios_disponibles"), texto("Tenemos Corte de cabello $15.000")]
    enviar("¿Qué servicios tienen?")
    assert modelos.forzado.llamadas == [] and len(modelos.principal.llamadas) == 2


def test_con_varios_empleados_primero_se_pregunta_con_quien(negocio, equipo, modelos, monkeypatch):
    activar(monkeypatch, "servicios")
    modelos.principal.respuestas = [texto("¿Con quién prefieres la cita?")]
    assert enviar("¿Qué servicios tienen?")[0] == "¿Con quién prefieres la cita?"
    assert modelos.forzado.llamadas == []


# --- T3.2: enrutamiento --------------------------------------------------------------------------------------------

def test_el_relevo_de_una_consulta_usa_el_modelo_simple(negocio, modelos, monkeypatch):
    activar(monkeypatch, "", "relay", "gpt-4.1-nano")
    registrar = MagicMock()
    monkeypatch.setattr(g, "registrar_uso", registrar)
    uso = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    modelos.principal.respuestas = [AIMessage(content="", tool_calls=llamada("consultar_servicios_disponibles").tool_calls,
                                              usage_metadata=uso)]
    modelos.simple.respuestas = [AIMessage(content="Tenemos Corte de cabello $15.000", usage_metadata=uso)]

    assert enviar("¿Qué ofrecen?")[0] == "Tenemos Corte de cabello $15.000"
    assert len(modelos.principal.llamadas) == 1 and len(modelos.simple.llamadas) == 1
    llamadas = [(c.kwargs["tipo"], c.kwargs["modelo"]) for c in registrar.call_args_list]
    assert llamadas == [("turno", g.CHAT_MODEL_NAME), ("relevo", "gpt-4.1-nano")]


def test_lo_que_decide_o_maneja_dinero_queda_con_el_modelo_principal(negocio, modelos, monkeypatch):
    activar(monkeypatch, "", "relay", "gpt-4.1-nano")
    modelos.principal.respuestas = [
        llamada("seleccionar_empleado", {"employee_id": "emp-1"}),  # su resultado lo relata el modelo simple
        texto("Listo"),  # el resultado de crear_cita lo relata el principal
    ]
    modelos.simple.respuestas = [llamada("crear_cita", {"servicio_nombre": "Corte de cabello",
                                                        "fecha_hora": f"{SABADO}T10:00:00", "nombre_cliente": "Ana"})]
    enviar("Agéndame el corte el sábado a las 10, soy Ana")
    # seleccionar_empleado -> relevo (simple); crear_cita -> el resultado lo relata el principal.
    assert len(modelos.simple.llamadas) == 1
    assert len(modelos.principal.llamadas) == 2


def test_sin_modelo_simple_configurado_todo_va_al_principal(negocio, modelos, monkeypatch):
    activar(monkeypatch, "", "relay", "")
    monkeypatch.setattr(g, "_model_simple", None)
    modelos.principal.respuestas = [llamada("consultar_servicios_disponibles"), texto("Lista")]
    assert enviar("¿Qué ofrecen?")[0] == "Lista" and len(modelos.principal.llamadas) == 2


def test_el_enrutamiento_esta_apagado_por_defecto():
    from core import settings

    assert settings.CHAT_FLUJOS_DETERMINISTAS == "" and settings.CHAT_ENRUTAMIENTO == ""
    assert settings.CHAT_MODEL_NAME_SIMPLE == ""
    assert g._model_simple is None


def test_el_modelo_simple_se_construye_con_las_mismas_herramientas(monkeypatch):
    """Se importa el modulo de nuevo con un nombre configurado para cubrir la construccion del modelo."""
    import importlib

    from core import settings

    monkeypatch.setattr(settings, "CHAT_MODEL_NAME_SIMPLE", "gpt-4.1-nano")
    recargado = importlib.reload(g)
    try:
        assert recargado._model_simple is not None
    finally:
        monkeypatch.setattr(settings, "CHAT_MODEL_NAME_SIMPLE", "")
        importlib.reload(g)
