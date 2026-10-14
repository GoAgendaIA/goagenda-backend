import re
import time
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from agent.prompts import build_system_prompt
from agent.flujos import (
    TOOLS_DE_RELEVO_SEGURAS,
    emoji_del_negocio,
    es_saludo_puro,
    flujos_activos,
    pregunta_por_servicios,
)
from agent.prompts_v2 import build_system_prompt_v2
from agent.tools_schemas import esquemas_para_modelo
from agent.state import AgentState
from agent.tools import TOOLS, _formato_precio_cop, _normalizar, consultar_servicios_disponibles
from core.settings import (
    CHAT_MODEL_NAME,
    DATABASE_URL,
    CHAT_ENRUTAMIENTO,
    CHAT_FLUJOS_DETERMINISTAS,
    CHAT_HISTORIAL_MAX_TURNOS,
    CHAT_MODEL_NAME_SIMPLE,
    CHAT_PROMPT_VERSION,
    CHAT_TOOLS_VERSION,
    DB_POOL_MAX_SIZE,
    LLM_MAX_TOKENS_SALIDA,
    DB_POOL_MIN_SIZE,
    DB_POOL_TIMEOUT,
    OPENAI_API_KEY,
)
from services.ai_usage_tracking import registrar_uso
from services.conversation_tracking import marcar_conversacion, registrar_turno_conversacion
from services.db import (
    get_business_by_id,
    get_employee_services,
    get_employees,
    get_home_visit_zones,
    get_services,
    supabase as client_supabase,
)
from services.scheduling import DIAS_SEMANA_ES, ahora_local
from services.whatsapp import construir_mensaje_contacto_humano

MENSAJE_TRANSFERIDO = (
    "¡Perfecto! 🙌 En un momento una persona de nuestro equipo te atiende.\n\n"
    "Gracias por tu paciencia 😊"
)
MENSAJE_ERROR_TECNICO = (
    "Disculpa 🙏 estamos teniendo un problema técnico momentáneo.\n\n"
    "Por favor inténtalo de nuevo en un par de minutos 😊"
)
MENSAJE_LIMITE_RONDAS = "¡Ya casi terminamos! 😊 ¿Me confirmas de nuevo qué necesitas para continuar?"
MENSAJE_NEGOCIO_NO_DISPONIBLE = (
    "Este negocio todavia no tiene el agendamiento automatico activado. "
    "Contactalos directamente para agendar tu cita."
)

RECURSION_LIMIT = 12
LLM_MAX_INTENTOS = 3
LLM_ESPERA_ENTRE_INTENTOS_SEGUNDOS = 1.5
# Baja (default de la API es ~1.0) porque este agente necesita precision en
# datos concretos (fechas, horas, precios, ids) mas que creatividad - una
# temperatura alta aumenta el riesgo de que invente o redondee esos datos.
LLM_TEMPERATURE = 0.3

# Tools cuyo resultado el widget de chat puede ofrecer como botones de
# seleccion rapida (el cliente toca en vez de escribir).
_TOOLS_CON_OPCIONES = {
    "consultar_empleados_disponibles",
    "consultar_servicios_disponibles",
    "consultar_horas_disponibles",
    "pedir_confirmacion_cita",
}

# Pool y checkpointer se crean una sola vez al importar el modulo (mismo
# patron que el singleton `supabase` de services/db.py). setup() crea las
# tablas propias del checkpointer (checkpoints, checkpoint_writes, etc) la
# primera vez que corre; en corridas siguientes es un no-op seguro.
#
# Tamaño chico y configurable (ver core/settings.py): el pooler de Supabase
# admite 15 clientes en total entre todos los procesos, y con el default de
# 4-10 conexiones por proceso se agotaba. check_connection verifica cada
# conexion antes de entregarla y descarta las que la red o el pooler
# cerraron mientras estaban inactivas (antes se entregaban sin revisar y
# la primera peticion tras un rato sin trafico podia fallar). max_idle mas
# corto tambien evita guardar conexiones inactivas que nadie usa.
_pool = ConnectionPool(
    conninfo=DATABASE_URL,
    kwargs={"autocommit": True, "row_factory": dict_row},
    open=True,
    min_size=DB_POOL_MIN_SIZE,
    max_size=DB_POOL_MAX_SIZE,
    timeout=DB_POOL_TIMEOUT,
    max_idle=300,
    check=ConnectionPool.check_connection,
)
_checkpointer = PostgresSaver(_pool)
_checkpointer.setup()

# max_retries=0: los reintentos ya los hace _invocar_modelo (LLM_MAX_INTENTOS).
# Con max_retries=3 aqui, un fallo se multiplicaba hasta 9 intentos.
# max_tokens acota la respuesta (las tool calls tambien cuentan).
_llm = ChatOpenAI(
    model=CHAT_MODEL_NAME,
    api_key=OPENAI_API_KEY,
    temperature=LLM_TEMPERATURE,
    max_retries=0,
    max_tokens=LLM_MAX_TOKENS_SALIDA,
)
_TOOLS_PARA_MODELO = esquemas_para_modelo(TOOLS, CHAT_TOOLS_VERSION)
_model = _llm.bind_tools(_TOOLS_PARA_MODELO)
# Mismo modelo, pero obligado a llamar consultar_servicios_disponibles: se
# usa solo cuando _precios_no_respaldados detecta que la respuesta normal
# menciono precios que no existen (ver _agent_node).
_model_forzar_servicios = _llm.bind_tools(_TOOLS_PARA_MODELO, tool_choice="consultar_servicios_disponibles")

# Enrutamiento de modelo (T3.2): modelo barato para las llamadas que solo relatan
# un resultado. Se construye solo si hay un nombre configurado; con los mismos
# parametros y las mismas herramientas que el principal.
_model_simple = None
if CHAT_MODEL_NAME_SIMPLE:
    _model_simple = ChatOpenAI(
        model=CHAT_MODEL_NAME_SIMPLE,
        api_key=OPENAI_API_KEY,
        temperature=LLM_TEMPERATURE,
        max_retries=0,
        max_tokens=LLM_MAX_TOKENS_SALIDA,
    ).bind_tools(_TOOLS_PARA_MODELO)

_PATRON_PRECIO = re.compile(r"\$\s?(\d{1,3}(?:[.,]\d{3})+|\d+)")
_PATRON_NUMERO = re.compile(r"\d+(?:[.,]\d+)*")


def _a_entero(texto: str) -> int | None:
    """
    '15.000' / '15,000' / '15000' / '15000.0' -> 15000. Los grupos de miles
    siempre tienen 3 digitos; un final de 1-2 digitos tras el separador es
    la parte decimal (ej. el '.0' de un float) y se descarta.
    """
    limpio = re.sub(r"[.,]\d{1,2}$", "", texto)
    digitos = re.sub(r"[.,]", "", limpio)
    return int(digitos) if digitos.isdigit() else None


def _precios_no_respaldados(texto: str, mensajes: list, catalogo: list[dict], business_id: str) -> list[int]:
    """
    Precios ("$15.000") que el modelo escribio en su respuesta y que no salen
    de ningun dato real: ni del catalogo de servicios, ni de recargos de
    domicilio (o precio + recargo), ni de lo que devolvieron las tools en
    esta conversacion (resumenes, abonos en centavos, etc). Una lista no
    vacia significa que esta inventando (ej. un saludo con servicios y
    precios que no existen, sin haber consultado nada).
    """
    mencionados = {_a_entero(m) for m in _PATRON_PRECIO.findall(texto)} - {None}
    if not mencionados:
        return []

    precios = {int(round(float(s["price"]))) for s in catalogo if s.get("price") is not None}
    recargos = {int(round(float(z["fee"]))) for z in get_home_visit_zones(business_id) if z.get("fee") is not None}
    validos = precios | recargos | {p + r for p in precios for r in recargos} | {0}
    for m in mensajes:
        if getattr(m, "type", None) == "tool":
            for numero in _PATRON_NUMERO.findall(str(m.content)):
                valor = _a_entero(numero)
                if valor is not None:
                    validos.update({valor, valor // 100})
    return sorted(mencionados - validos)


def _construir_horario_texto(business_id: str) -> str:
    horario_response = (
        client_supabase.table("business_hours").select("*").eq("business_id", business_id).execute()
    )
    dias_map = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    horario_por_dia = {row["day"]: row for row in horario_response.data}

    lineas = []
    for dia in dias_map:
        info = horario_por_dia.get(dia)
        if not info or not info.get("is_open"):
            lineas.append(f"{DIAS_SEMANA_ES[dia]}: cerrado")
        else:
            linea = f"{DIAS_SEMANA_ES[dia]}: {info['opening_time'][:5]} a {info['closing_time'][:5]}"
            if info.get("lunch_start") and info.get("lunch_end"):
                linea += f" (almuerzo {info['lunch_start'][:5]} a {info['lunch_end'][:5]}, no se agenda en ese rango)"
            lineas.append(linea)
    return "; ".join(lineas)


def _historial_valido_para_modelo(messages: list) -> list:
    """
    Reconstruye el historial en el orden que exige la API de OpenAI: cada
    AIMessage con tool_calls debe ir seguido inmediatamente de un
    ToolMessage por cada tool_call_id. Si un tool step se cae a mitad de
    camino (crash del proceso, excepcion no controlada, etc.) el
    checkpoint puede quedar con tool_calls sin respuesta, o con la
    respuesta agregada al final en vez de justo despues. Sin este
    arreglo, la sesion queda invalida para siempre: OpenAI rechaza el
    mismo historial en cada turno futuro y el bot repite el mensaje de
    error tecnico sin parar.
    """
    tool_messages_por_id = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    resultado = []
    for mensaje in messages:
        if isinstance(mensaje, ToolMessage):
            continue  # se reinsertan justo despues de su AIMessage, abajo
        resultado.append(mensaje)
        if isinstance(mensaje, AIMessage) and mensaje.tool_calls:
            for tool_call in mensaje.tool_calls:
                tool_msg = tool_messages_por_id.get(tool_call["id"])
                if tool_msg is None:
                    tool_msg = ToolMessage(
                        content="Hubo un problema tecnico ejecutando esta accion, ignorala.",
                        tool_call_id=tool_call["id"],
                    )
                resultado.append(tool_msg)
    return resultado


# Resultados de consulta que quedan "viejos" en cuanto se vuelve a consultar lo
# mismo (horas libres, empleados, servicios, zonas). Solo el mas reciente de cada
# tipo le sirve al modelo; los anteriores se reenviaban completos en cada llamada.
_TOOLS_CON_RESULTADO_OBSOLESCIBLE = {
    "consultar_horas_disponibles",
    "consultar_empleados_disponibles",
    "consultar_servicios_disponibles",
    "consultar_zonas_domicilio",
}
_RESULTADO_OBSOLETO = "(resultado anterior omitido: mas abajo en esta conversacion hay una consulta mas reciente)"
_AVISO_HISTORIAL_RECORTADO = (
    "(Los mensajes mas antiguos de esta conversacion se omitieron por brevedad. Los datos vigentes estan en el "
    "ESTADO ACTUAL del final de las instrucciones; si te falta algun dato del cliente, como su nombre, pideselo.)"
)


def _compactar_resultados_obsoletos(mensajes: list) -> list:
    """Reemplaza por un marcador los ToolMessage de consulta que ya fueron superados por uno mas reciente."""
    ultimo_por_tool: dict[str, int] = {}
    for i, m in enumerate(mensajes):
        if isinstance(m, ToolMessage) and m.name in _TOOLS_CON_RESULTADO_OBSOLESCIBLE:
            ultimo_por_tool[m.name] = i

    resultado = []
    for i, m in enumerate(mensajes):
        if isinstance(m, ToolMessage) and m.name in ultimo_por_tool and ultimo_por_tool[m.name] != i:
            m = m.model_copy(update={"content": _RESULTADO_OBSOLETO})
        resultado.append(m)
    return resultado


def _limitar_turnos(mensajes: list, max_turnos: int) -> list:
    """
    Conserva solo los ultimos max_turnos turnos del cliente. Siempre corta en un
    mensaje HUMANO: asi nunca queda un ToolMessage sin el AIMessage que lo pidio
    (la API de OpenAI rechazaria ese historial).
    """
    if max_turnos <= 0:
        return mensajes
    inicios = [i for i, m in enumerate(mensajes) if isinstance(m, HumanMessage)]
    if len(inicios) <= max_turnos:
        return mensajes
    return [SystemMessage(content=_AVISO_HISTORIAL_RECORTADO)] + mensajes[inicios[-max_turnos]:]


def _preparar_historial_para_modelo(mensajes: list) -> list:
    """Historial que realmente viaja al modelo: valido, sin resultados obsoletos y acotado en turnos."""
    return _limitar_turnos(
        _compactar_resultados_obsoletos(_historial_valido_para_modelo(mensajes)), CHAT_HISTORIAL_MAX_TURNOS
    )


def _resultados_recientes(mensajes: list) -> list:
    """Los ToolMessage que acaban de ejecutarse (los ultimos, hasta el primer mensaje que no es de herramienta)."""
    recientes = []
    for m in reversed(mensajes):
        if not isinstance(m, ToolMessage):
            break
        recientes.append(m)
    return list(reversed(recientes))


def _elegir_modelo(state: AgentState, empleados: list[dict]):
    """
    (modelo, nombre, tipo_de_llamada) para la llamada que sigue.
    - T3.1 "servicios": si el cliente solo pregunta que servicios hay o cuanto
      cuestan, la primera llamada ya es la consulta (antes se generaba un texto
      que la red anti-alucinacion descartaba y se llamaba de nuevo). Solo si no
      hay que elegir empleado antes.
    - T3.2 "relay": si lo ultimo fue un resultado de consulta que solo hay que
      relatar, se usa el modelo barato. Lo que maneja dinero o decide (crear,
      cancelar, reprogramar, escalar) queda con el principal.
    """
    mensajes = state["messages"]
    flujos = flujos_activos(CHAT_FLUJOS_DETERMINISTAS)

    if (
        "servicios" in flujos
        and mensajes
        and isinstance(mensajes[-1], HumanMessage)
        and pregunta_por_servicios(mensajes[-1].content)
        and (len(empleados) <= 1 or state.get("employee_id"))
    ):
        return _model_forzar_servicios, CHAT_MODEL_NAME, "turno_servicios_directo"

    if CHAT_ENRUTAMIENTO == "relay" and _model_simple is not None:
        recientes = _resultados_recientes(mensajes)
        if recientes and all(m.name in TOOLS_DE_RELEVO_SEGURAS for m in recientes):
            return _model_simple, CHAT_MODEL_NAME_SIMPLE, "relevo"

    return _model, CHAT_MODEL_NAME, "turno"


def _datos_para_saludo(state: AgentState):
    """(negocio, empleado, servicios) si el saludo se puede responder en codigo; si no, None."""
    business = get_business_by_id(state["business_id"])
    if not business:
        return None
    empleados = get_employees(state["business_id"])
    employee_id = state.get("employee_id")
    if state.get("employee_fijo") and employee_id:
        empleado = next((e for e in empleados if e["id"] == employee_id), None)
    elif len(empleados) == 1:
        empleado = empleados[0]
    else:
        return None  # con varios empleados primero hay que preguntar con quien: lo decide el modelo
    if not empleado:
        return None
    servicios = get_employee_services(empleado["id"])
    if not servicios:
        return None
    return business, empleado, servicios


def _ruta_inicial(state: AgentState) -> str:
    """Entrada del grafo: un saludo puro al inicio de la conversacion se responde sin modelo (T3.1)."""
    if "saludo" not in flujos_activos(CHAT_FLUJOS_DETERMINISTAS):
        return "agent"
    if state.get("transferido") or state.get("escalado"):
        return "agent"
    mensajes = state["messages"]
    if not mensajes or not isinstance(mensajes[-1], HumanMessage):
        return "agent"
    if any(isinstance(m, AIMessage) for m in mensajes[:-1]):
        return "agent"  # ya hubo conversacion: no se vuelve a presentar la lista
    if not es_saludo_puro(mensajes[-1].content):
        return "agent"
    return "saludo" if _datos_para_saludo(state) is not None else "agent"


def _nodo_saludo(state: AgentState) -> dict:
    """
    Responde el saludo con la lista REAL de servicios, igual que lo pide el
    prompt (nombre del negocio + servicios con precio y duracion + una
    pregunta) pero sin llamar al modelo. Deja en el historial el mismo par
    llamada/resultado de consultar_servicios_disponibles que habria dejado el
    modelo, para que lo que sigue (botones, red de precios, turnos futuros) no
    note la diferencia.
    """
    business, empleado, servicios = _datos_para_saludo(state)
    llamada_id = f"call_{uuid.uuid4().hex[:24]}"
    actualizacion = consultar_servicios_disponibles.func(state["business_id"], empleado["id"], llamada_id).update

    nombre = business.get("name") or "el negocio"
    emoji = emoji_del_negocio(business.get("business_type"))
    if state.get("employee_fijo") and state.get("employee_id"):
        lineas = [f"¡Hola! 👋 Bienvenido a {nombre}, este enlace de chat pertenece a {empleado.get('name') or 'nuestro equipo'}."]
    else:
        lineas = [f"¡Hola! 👋 Bienvenido a {nombre}."]
    lineas.append("Estos son nuestros servicios:")
    for s in servicios:
        lineas.append(f"{emoji} *{s['name']}* - {s['duration_minutes']} min - {_formato_precio_cop(s['price'])}")
    lineas.append("¿Cuál te interesa o quieres agendar una cita?")

    llamada = AIMessage(
        content="", tool_calls=[{"name": "consultar_servicios_disponibles", "args": {}, "id": llamada_id, "type": "tool_call"}]
    )
    return {
        "messages": [llamada, *actualizacion["messages"], AIMessage(content="\n".join(lineas))],
        "ultimas_opciones": actualizacion.get("ultimas_opciones"),
        "employee_id": empleado["id"],
    }


def _es_resumen_de_confirmacion(mensaje) -> bool:
    """
    True solo para el resumen EXITOSO de pedir_confirmacion_cita. No basta el
    nombre de la tool: ToolNode tambien le pone ese nombre a sus mensajes de
    error ("falta el WhatsApp", "esa hora no esta disponible"...), que debe
    explicarle el modelo. El resumen siempre termina en la pregunta de
    confirmacion (el mismo criterio de _opciones_coherentes).
    """
    return (
        getattr(mensaje, "name", None) == "pedir_confirmacion_cita"
        and _normalizar(str(mensaje.content)).rstrip().endswith("confirmo tu cita?")
    )


def _despues_de_tools(state: AgentState) -> str:
    """
    Tras ejecutar herramientas: el resumen de pedir_confirmacion_cita ya es el
    texto final (el prompt le exige al modelo entregarlo tal cual), asi que se
    responde sin una llamada mas (T3.1). Solo el caso exitoso.
    """
    if "confirmacion" in flujos_activos(CHAT_FLUJOS_DETERMINISTAS):
        recientes = _resultados_recientes(state["messages"])
        if len(recientes) == 1 and _es_resumen_de_confirmacion(recientes[0]):
            return "directo"
    return "agent"


def _nodo_respuesta_directa(state: AgentState) -> dict:
    return {"messages": [AIMessage(content=str(_resultados_recientes(state["messages"])[0].content))]}


def _agent_node(state: AgentState) -> dict:
    if state.get("transferido"):
        return {"messages": [AIMessage(content=MENSAJE_TRANSFERIDO)]}

    business = get_business_by_id(state["business_id"])
    if not business:
        return {"messages": [AIMessage(content=MENSAJE_ERROR_TECNICO)]}

    horario_texto = _construir_horario_texto(state["business_id"])
    # Sin minutos: al ir en el prompt, un valor que cambia cada minuto invalida
    # la cache de prefijo de OpenAI; la hora exacta la validan las tools.
    fecha_actual = ahora_local().strftime("%Y-%m-%d %H:00 (%A)")
    empleados = [
        {
            "id": e["id"],
            "name": e.get("name") or "Sin nombre",
            "servicios": [s["name"] for s in get_employee_services(e["id"])],
        }
        for e in get_employees(state["business_id"])
    ]
    catalogo = get_employee_services(state["employee_id"]) if state.get("employee_id") else get_services(state["business_id"])
    construir_prompt = build_system_prompt_v2 if CHAT_PROMPT_VERSION == "v2" else build_system_prompt
    system = construir_prompt(
        business,
        horario_texto,
        fecha_actual,
        state.get("client_phone"),
        empleados,
        state.get("employee_id"),
        state.get("employee_fijo", False),
        state.get("service_id"),
        state.get("escalado", False),
        catalogo=catalogo,
    )

    entrada = [SystemMessage(content=system)] + _preparar_historial_para_modelo(state["messages"])

    modelo_turno, nombre_modelo, tipo_llamada = _elegir_modelo(state, empleados)
    response = _invocar_modelo(
        modelo_turno, entrada, state["business_id"], tipo=tipo_llamada, session_id=state.get("session_id"),
        nombre_modelo=nombre_modelo,
    )
    if response is None:
        mensaje = MENSAJE_ERROR_TECNICO + "\n\n" + construir_mensaje_contacto_humano(business)
        return {"messages": [AIMessage(content=mensaje)]}

    # Red de seguridad contra servicios/precios inventados: si la respuesta
    # final (sin tool calls) menciona precios que no salen de ningun dato
    # real, se descarta y se obliga al modelo a consultar los servicios
    # reales primero - el grafo vuelve a pasar por aqui con el resultado de
    # la tool y responde con datos verdaderos. Solo se fuerza una vez por
    # turno (si ya consulto servicios despues del ultimo mensaje del
    # cliente, no se vuelve a forzar, para no entrar en un ciclo).
    if not getattr(response, "tool_calls", None) and not _consulto_servicios_en_este_turno(state["messages"]):
        texto = str(response.content)
        inventados = _precios_no_respaldados(texto, state["messages"], catalogo, state["business_id"])
        listados = _servicios_mencionados(texto, catalogo)
        # Enumerar 2+ servicios es mostrar el menu: tiene que pasar por la
        # tool, que es la que da los botones de seleccion rapida y los
        # datos verdaderos de duracion/precio. Excepto si esta listando
        # EMPLEADOS (cada uno con sus servicios): eso es elegir con quien,
        # y forzar la tool de servicios ahi desviaria la conversacion.
        nombres_empleados = [_normalizar(e["name"]) for e in empleados if e.get("name") and e["name"] != "Sin nombre"]
        lista_empleados = sum(1 for n in nombres_empleados if n in _normalizar(texto)) >= 2
        if inventados or (len(listados) >= 2 and not lista_empleados):
            motivo = f"precios sin respaldo={inventados}" if inventados else f"lista servicios sin consultar={listados}"
            print(
                f"[anti-alucinacion] negocio={state['business_id']} {motivo}; "
                f"se descarta la respuesta y se fuerza consultar_servicios_disponibles"
            )
            forzada = _invocar_modelo(
                _model_forzar_servicios,
                entrada,
                state["business_id"],
                tipo="re_llamada_anti_alucinacion",
                session_id=state.get("session_id"),
            )
            if forzada is not None and getattr(forzada, "tool_calls", None):
                response = forzada

    return {"messages": [response]}


def _servicios_mencionados(texto: str, catalogo: list[dict]) -> list[str]:
    """Nombres del catalogo que aparecen en el texto (sin importar tildes ni mayusculas)."""
    texto_normalizado = _normalizar(texto)
    return [s["name"] for s in catalogo if s.get("name") and _normalizar(s["name"]) in texto_normalizado]


def _consulto_servicios_en_este_turno(mensajes: list) -> bool:
    """True si despues del ultimo mensaje del cliente ya se llamo consultar_servicios_disponibles."""
    for m in reversed(mensajes):
        if getattr(m, "type", None) == "human":
            return False
        if getattr(m, "type", None) == "tool" and getattr(m, "name", None) == "consultar_servicios_disponibles":
            return True
    return False


def _invocar_modelo(
    modelo, entrada: list, business_id: str, tipo: str = "turno", session_id: str | None = None,
    nombre_modelo: str | None = None,
):
    """
    Invoca el modelo y registra el consumo de tokens. Reintenta hasta
    LLM_MAX_INTENTOS veces antes de rendirse (devuelve None): la falla
    observada en produccion (un solo intento fallido justo despues de que
    el cliente dio su nombre y numero) se resolvio sola al repetir el mismo
    mensaje segundos despues, señal de que era transitoria (timeout/hiccup
    de red), no un error real de la solicitud.
    """
    response = None
    for intento in range(1, LLM_MAX_INTENTOS + 1):
        try:
            response = modelo.invoke(entrada)
            break
        except Exception as e:
            print(f"Error invocando al modelo de IA (intento {intento}/{LLM_MAX_INTENTOS}): {e}")
            if intento < LLM_MAX_INTENTOS:
                time.sleep(LLM_ESPERA_ENTRE_INTENTOS_SEGUNDOS)

    if response is not None:
        usage = getattr(response, "usage_metadata", None) or {}
        if usage:
            detalle = usage.get("input_token_details") or {}
            registrar_uso(
                business_id,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("total_tokens", 0),
                cached_tokens=detalle.get("cache_read", 0) or 0,
                tipo=tipo,
                session_id=session_id,
                modelo=nombre_modelo,
            )
    return response


def _build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("agent", _agent_node)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_node("saludo", _nodo_saludo)
    builder.add_node("respuesta_directa", _nodo_respuesta_directa)
    builder.add_conditional_edges(START, _ruta_inicial, {"saludo": "saludo", "agent": "agent"})
    builder.add_edge("saludo", END)
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    builder.add_conditional_edges("tools", _despues_de_tools, {"agent": "agent", "directo": "respuesta_directa"})
    builder.add_edge("respuesta_directa", END)
    return builder.compile(checkpointer=_checkpointer)


GRAPH = _build_graph()


def _thread_config(business_id: str, session_id: str) -> dict:
    # El business_id como prefijo del thread_id es lo que aisla las
    # conversaciones por negocio: aunque dos negocios usen el mismo
    # session_id, sus hilos quedan en claves distintas en el checkpointer.
    return {
        "configurable": {"thread_id": f"{business_id}:{session_id}"},
        "recursion_limit": RECURSION_LIMIT,
    }


def _opciones_coherentes(
    nombre_tool: str, opciones: list[dict], respuesta_texto: str, textos_cliente: list[str]
) -> list[dict] | None:
    """
    Los botones salen de la ULTIMA tool del turno, pero el texto final del
    modelo puede estar preguntando otra cosa (ej. pide la fecha o el nombre
    despues de haber consultado los servicios o armado un resumen): el
    cliente veia botones que no correspondian a la pregunta. Solo se dejan
    si el texto que ve el cliente realmente trata de esas opciones.
    """
    respuesta = _normalizar(respuesta_texto)
    if nombre_tool == "pedir_confirmacion_cita":
        return opciones if "confirmo tu cita" in respuesta else None

    valores = [_normalizar(str(o.get("value", ""))) for o in opciones]
    valores = [v for v in valores if v]

    if nombre_tool == "consultar_servicios_disponibles":
        # Si el cliente ya nombro un servicio, no hay nada que elegir de nuevo.
        texto_cliente = _normalizar(" ".join(textos_cliente))
        if any(v in texto_cliente for v in valores):
            return None

    # Horas ("9:00 am"): basta con que aparezca la hora sin el am/pm.
    claves = {v for v in valores} | {v.split()[0] for v in valores}
    return opciones if any(clave in respuesta for clave in claves) else None


def _extraer_opciones(
    mensajes_nuevos: list,
    ultimas_opciones: list[dict] | None,
    service_id: str | None,
    employee_id: str | None,
    respuesta_texto: str = "",
    textos_cliente: list[str] | None = None,
) -> list[dict] | None:
    """
    Opciones de seleccion rapida (botones) para el widget de chat, solo si
    la ULTIMA tool ejecutada en este turno fue una de listado (empleados,
    servicios u horas). Si el turno no llamo ninguna, o la ultima fue otra
    (ej. seleccionar_empleado despues de listar, o crear_cita), no se
    muestran botones — evita ofrecer opciones "viejas" que ya no aplican.

    Ademas, si el cliente ya confirmo un servicio/empleado (service_id o
    employee_id ya en el estado), nunca se vuelven a mostrar los botones
    de esa lista, aunque el bot vuelva a llamar la tool de listado en ese
    mismo turno (ej. para confirmar precio/duracion antes de agendar) —
    sin esto, cualquier re-consulta hacia el final del flujo hacia
    reaparecer el selector completo como si el cliente tuviera que volver
    a elegir.
    """
    for mensaje in reversed(mensajes_nuevos):
        if isinstance(mensaje, ToolMessage):
            if mensaje.name not in _TOOLS_CON_OPCIONES:
                return None
            if mensaje.name == "consultar_servicios_disponibles" and service_id:
                return None
            if mensaje.name == "consultar_empleados_disponibles" and employee_id:
                return None
            if not ultimas_opciones:
                return None
            return _opciones_coherentes(mensaje.name, ultimas_opciones, respuesta_texto, textos_cliente or [])
    return None


MENSAJE_PROCESANDO = "Estoy terminando de procesar tu mensaje anterior 🙏 Dame un momento y vuelve a escribirme."


def _mensaje_error_tecnico(business_id: str) -> str:
    """
    MENSAJE_ERROR_TECNICO + como contactar al negocio. Si hasta eso falla
    (ej. Supabase tambien caido), devuelve solo el mensaje base: este helper
    se usa justamente en los caminos de error y no puede lanzar excepciones
    (si lanzara, el endpoint responderia 500 y el widget mostraria "No se
    pudo enviar el mensaje" en vez de una respuesta).
    """
    try:
        return MENSAJE_ERROR_TECNICO + "\n\n" + construir_mensaje_contacto_humano(get_business_by_id(business_id))
    except Exception as e:
        print(f"No se pudo armar el mensaje de contacto del negocio {business_id}: {e}")
        return MENSAJE_ERROR_TECNICO


def _respuesta_de_turno(mensajes: list, desde: int, ultimas_opciones, service_id, employee_id) -> tuple[str, list[dict] | None]:
    """(texto, opciones) de la respuesta del asistente en los mensajes posteriores a `desde`."""
    mensajes_nuevos = mensajes[desde:]
    respuesta_texto = next(
        (m.content for m in reversed(mensajes_nuevos) if isinstance(m, AIMessage) and m.content), ""
    )
    textos_cliente = [str(m.content) for m in mensajes if isinstance(m, HumanMessage)]
    opciones = _extraer_opciones(
        mensajes_nuevos, ultimas_opciones, service_id, employee_id, respuesta_texto, textos_cliente
    )
    return respuesta_texto, opciones


def enviar_mensaje(
    business_id: str,
    session_id: str,
    mensaje: str,
    employee_id: str | None = None,
    client_message_id: str | None = None,
) -> tuple[str, list[dict] | None]:
    """
    Manda un mensaje del cliente al agente y devuelve (respuesta_en_texto,
    opciones_de_seleccion_rapida). Si employee_id viene dado (chat propio
    de un empleado), se manda en CADA invocacion del grafo, igual que
    business_id: asi queda fijo de forma confiable sin depender de que el
    modelo no intente cambiarlo.

    client_message_id (opcional, lo genera el widget) hace el envio
    idempotente: si la respuesta se pierde en el camino (red, timeout) y el
    widget reintenta con el mismo id, se devuelve la respuesta que ya se
    habia generado en vez de procesar el mensaje otra vez - sin esto, un
    reintento de un "Si" podia, por ejemplo, intentar agendar dos veces.

    Nunca lanza excepciones: cualquier falla (incluida la base de datos del
    checkpointer) se convierte en un mensaje amable dentro del chat. Antes
    GRAPH.get_state estaba fuera del try, y si el pool de conexiones no
    podia dar una conexion el endpoint respondia 500 y el widget mostraba
    "No se pudo enviar el mensaje".
    """
    config = _thread_config(business_id, session_id)

    try:
        snapshot_previo = GRAPH.get_state(config)
        valores_previos = snapshot_previo.values if snapshot_previo and snapshot_previo.values else {}
        mensajes_anteriores = valores_previos.get("messages", [])

        if client_message_id:
            indice = next(
                (i for i, m in enumerate(mensajes_anteriores) if isinstance(m, HumanMessage) and m.id == client_message_id),
                None,
            )
            if indice is not None:
                print(f"[chat] reintento del mensaje {client_message_id} (thread {business_id}:{session_id}): no se reprocesa")
                respuesta_texto, opciones = _respuesta_de_turno(
                    mensajes_anteriores,
                    indice + 1,
                    valores_previos.get("ultimas_opciones"),
                    valores_previos.get("service_id"),
                    valores_previos.get("employee_id"),
                )
                return (respuesta_texto, opciones) if respuesta_texto else (MENSAJE_PROCESANDO, None)

        entrada = {
            "messages": [HumanMessage(content=mensaje, id=client_message_id)],
            "business_id": business_id,
            "session_id": session_id,
        }
        if employee_id:
            entrada["employee_id"] = employee_id
            entrada["employee_fijo"] = True

        if not valores_previos:
            # Primer turno de este thread: varias tools (transferir_a_equipo,
            # escalar_por_confusion, crear_cita, consultar_servicios_disponibles,
            # etc.) leen client_phone/employee_id via InjectedState, que lanza
            # KeyError si esa clave nunca se escribio en el estado - ej. el
            # cliente pide hablar con un asesor antes de dar su numero, o
            # pregunta por servicios antes de elegir empleado en el chat
            # general. setdefault no pisa el employee_id real si ya se puso
            # arriba (chat de un empleado fijo).
            entrada.setdefault("client_phone", None)
            entrada.setdefault("employee_id", None)

        registrar_turno_conversacion(business_id, session_id)
        resultado = GRAPH.invoke(entrada, config=config)
    except GraphRecursionError:
        try:
            business = get_business_by_id(business_id)
            return MENSAJE_LIMITE_RONDAS + "\n\n" + construir_mensaje_contacto_humano(business), None
        except Exception:
            return MENSAJE_LIMITE_RONDAS, None
    except Exception as e:
        # Cualquier excepcion no prevista (bug en una tool, base de datos
        # del checkpointer sin conexiones disponibles, etc.): mensaje amable
        # en vez de un 500 crudo que deja el widget roto.
        print(f"Error inesperado en el chat (thread {business_id}:{session_id}): {type(e).__name__}: {e}")
        return _mensaje_error_tecnico(business_id), None

    respuesta_texto, opciones = _respuesta_de_turno(
        resultado["messages"],
        len(mensajes_anteriores),
        resultado.get("ultimas_opciones"),
        resultado.get("service_id"),
        resultado.get("employee_id"),
    )
    if respuesta_texto:
        return respuesta_texto, opciones
    return MENSAJE_ERROR_TECNICO, None


def enviar_respuesta_humana(business_id: str, session_id: str, mensaje: str) -> None:
    """
    Escribe la respuesta manual de un dueño/empleado directo en el
    checkpointer de esa conversacion, como si fuera un mensaje del
    asistente, sin invocar el modelo. Tambien marca transferido=True: a
    partir de aqui el bot deja de responder en esta conversacion (mismo
    mecanismo que ya usa transferir_a_equipo), para que el bot y el
    humano no le contesten al cliente al mismo tiempo.
    """
    config = _thread_config(business_id, session_id)
    GRAPH.update_state(config, {"messages": [AIMessage(content=mensaje)], "transferido": True})
    marcar_conversacion(business_id, session_id, handed_off=True)


def enviar_notificacion_sistema(business_id: str, session_id: str, mensaje: str) -> None:
    """
    Igual que enviar_respuesta_humana (escribe un mensaje del asistente
    directo en el checkpointer, sin invocar el modelo), pero SIN marcar
    transferido=True: el bot sigue respondiendo con normalidad despues. Se
    usa para mensajes automaticos del sistema que no implican que un
    humano tomo la conversacion, ej. la confirmacion de un pago de Wompi
    (ver routes/wompi_webhook_routes.py). Si el thread no existe todavia
    (session_id invalido o conversacion nunca iniciada), no hace nada.
    """
    config = _thread_config(business_id, session_id)
    if not GRAPH.get_state(config).values:
        return
    GRAPH.update_state(config, {"messages": [AIMessage(content=mensaje)]})


def obtener_historial(business_id: str, session_id: str) -> tuple[list[dict], list[dict] | None]:
    """
    Lee el historial persistido de una sesion (solo turnos humano/IA, sin
    tool calls), y si el ultimo mensaje es una respuesta del asistente,
    tambien recupera las opciones de seleccion rapida de ESE turno (si
    aplican) para que el widget pueda restaurarlas despues de un recargue
    de pagina (ej. el navegador recarga la pestaña al volver de segundo
    plano) - sin esto, las opciones se perdian porque son efimeras y solo
    viajaban en la respuesta directa de enviar_mensaje.
    """
    config = _thread_config(business_id, session_id)
    snapshot = GRAPH.get_state(config)
    if not snapshot or not snapshot.values:
        return [], None

    mensajes = snapshot.values.get("messages", [])
    historial = []
    for mensaje in mensajes:
        if isinstance(mensaje, HumanMessage) and mensaje.content:
            historial.append({"role": "user", "content": mensaje.content})
        elif isinstance(mensaje, AIMessage) and mensaje.content:
            historial.append({"role": "assistant", "content": mensaje.content})

    opciones = None
    if historial and historial[-1]["role"] == "assistant":
        ultimo_turno = []
        for mensaje in reversed(mensajes):
            ultimo_turno.insert(0, mensaje)
            if isinstance(mensaje, HumanMessage):
                break
        opciones = _extraer_opciones(
            ultimo_turno,
            snapshot.values.get("ultimas_opciones"),
            snapshot.values.get("service_id"),
            snapshot.values.get("employee_id"),
            historial[-1]["content"],
            [str(m.content) for m in mensajes if isinstance(m, HumanMessage)],
        )

    return historial, opciones
