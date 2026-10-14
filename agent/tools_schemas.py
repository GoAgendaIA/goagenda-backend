"""
Esquemas compactos de las herramientas para el modelo (T2.3).

Los esquemas de las 14 tools pesaban ~3.0 k tokens en CADA llamada porque las
docstrings mezclan la descripcion de la tool con reglas de flujo que el prompt
ya explica. La version "v2" conserva el comportamiento de cada tool pero con
descripciones cortas. Importante: el esquema base (nombres, tipos y
parametros requeridos) sale de la tool real, asi que NO puede desincronizarse;
solo se reemplazan las descripciones y se simplifican los opcionales
(`anyOf [T, null]` + `default` -> `T`).

Nota de diseño: NO se hace gating dinamico de herramientas por estado. Las
tools van al inicio del prefijo que OpenAI cachea; cambiar el conjunto de tools
entre llamadas invalida esa cache (justo lo que T1.1 consiguio) y el ahorro de
quitar unas pocas tools queda por debajo de lo que cuesta perder la cache.
"""

from langchain_core.utils.function_calling import convert_to_openai_tool

# nombre -> (descripcion de la tool, {parametro: descripcion})
DESCRIPCIONES_V2: dict[str, tuple[str, dict[str, str]]] = {
    "registrar_telefono_cliente": (
        "Guarda el WhatsApp del cliente: es la unica forma de identificarlo (nunca cedula ni telefono fijo). "
        "Usala apenas lo de.",
        {"numero_whatsapp": "WhatsApp del cliente, con o sin +57."},
    ),
    "consultar_empleados_disponibles": (
        "Lista los empleados activos con sus servicios, opcionalmente los que trabajan en una fecha. Usala antes de "
        "mostrar servicios/horas o agendar, salvo empleado fijo o uno solo.",
        {"fecha": "YYYY-MM-DD; filtra a quienes trabajan ese dia."},
    ),
    "seleccionar_empleado": (
        "Fija el empleado de la cita tras elegirlo (o automaticamente si hay uno solo). "
        "Servicios y horas se consultan para el.",
        {"employee_id": "Id del empleado (de consultar_empleados_disponibles)."},
    ),
    "consultar_servicios_disponibles": (
        "Servicios activos (los del empleado seleccionado, si hay) con precio y duracion.",
        {},
    ),
    "seleccionar_servicio": (
        "Fija el servicio de la cita apenas el cliente lo elija (nombre o boton), en el mismo turno en que "
        "resuelves el id. Evita volver a mostrar la lista.",
        {"service_id": "Id del servicio (de consultar_servicios_disponibles)."},
    ),
    "consultar_horas_disponibles": (
        "Horas libres de un dia para el empleado seleccionado. Usala al preguntar por horas o antes de sugerir una. "
        "Sin empleado seleccionado da error: usa consultar_empleados_disponibles y seleccionar_empleado.",
        {
            "fecha": "YYYY-MM-DD.",
            "servicio_nombre": "Servicio (para la duracion), opcional.",
        },
    ),
    "consultar_zonas_domicilio": (
        "Zonas donde hay domicilio y su recargo. Usala si preguntan donde llegan o cuanto cuesta, o antes de pedir "
        "la direccion. Lista vacia: sin restriccion ni recargo.",
        {},
    ),
    "pedir_confirmacion_cita": (
        "UNICA forma de pedir confirmacion antes de agendar (tambien si el cliente corrige un dato): arma el resumen "
        "y los botones Si/No. Muestralo tal cual; no llames crear_cita en el mismo turno.",
        {
            "servicio_nombre": "Nombre exacto.",
            "fecha_hora": "ISO 8601, ej: 2026-06-22T15:00:00.",
            "nombre_cliente": "Nombre.",
            "direccion": "Solo a domicilio.",
            "zona": "Solo a domicilio (de consultar_zonas_domicilio).",
        },
    ),
    "crear_cita": (
        "Agenda la cita validando horario y disponibilidad. Con abono NO la crea: genera un link de pago y se "
        "agenda al confirmarse; trae pago_pendiente=true en vez de cita_creada.",
        {
            "servicio_nombre": "Nombre exacto.",
            "fecha_hora": "ISO 8601, ej: 2026-06-22T15:00:00.",
            "nombre_cliente": "Nombre.",
            "direccion": "Solo a domicilio (la confirmada).",
            "zona": "Solo a domicilio (de consultar_zonas_domicilio).",
        },
    ),
    "consultar_citas_cliente": ("Citas confirmadas del cliente actual.", {}),
    "cancelar_cita": (
        "Cancela una cita. Si ya fue pagada NO cancela: devuelve requiere_confirmacion_por_pago con una advertencia "
        "que debes mostrar tal cual; solo si el cliente confirma, repite con confirmar_a_pesar_del_pago=true.",
        {
            "appointment_id": "Id de la cita.",
            "confirmar_a_pesar_del_pago": "False la primera vez; True solo tras confirmar la advertencia de pago.",
        },
    ),
    "reprogramar_cita": (
        "Cambia fecha/hora y/o modalidad (local <-> domicilio) de una cita EN LA MISMA cita (nunca crea otra). "
        "Pasa nueva_fecha_hora, direccion+zona, volver_al_local, o combinados. Sin appointment_id: "
        "consultar_citas_cliente.",
        {
            "appointment_id": "Id de la cita.",
            "nueva_fecha_hora": "ISO 8601; omitela si solo cambia la modalidad.",
            "direccion": "Nueva direccion si pasa a domicilio; nunca la inventes.",
            "zona": "Zona; solo con direccion.",
            "volver_al_local": "True solo si una cita a domicilio vuelve al local.",
        },
    ),
    "transferir_a_equipo": (
        "Usala YA cuando el cliente pida explicitamente hablar con una persona, el equipo, el dueño o alguien por "
        "su nombre. Detiene al bot.",
        {},
    ),
    "escalar_por_confusion": (
        "Usala si, tras aclarar una vez, no entiendes al cliente o ninguna tool te permite ayudarlo. Avisa al "
        "negocio en silencio (el cliente nunca debe enterarse). No en la primera duda ni repetida si ya "
        "notificaste; si pide hablar con alguien, transferir_a_equipo.",
        {},
    ),
}


def _simplificar_propiedad(propiedad: dict, descripcion: str | None) -> dict:
    """`anyOf [T, null]` + `default` -> `T` (opcional por no estar en required); descripcion nueva."""
    nueva = {k: v for k, v in propiedad.items() if k not in ("default", "title", "anyOf", "description")}
    if "anyOf" in propiedad:
        tipos = [o for o in propiedad["anyOf"] if o.get("type") != "null"]
        if tipos:
            nueva.update({k: v for k, v in tipos[0].items() if k != "title"})
    texto = descripcion if descripcion is not None else propiedad.get("description")
    if texto:
        nueva["description"] = texto
    return nueva


def esquemas_para_modelo(tools: list, version: str = "v1") -> list:
    """
    Lo que se le pasa a bind_tools. v1: las tools tal cual (comportamiento
    actual). v2: esquemas compactos derivados de las mismas tools; una tool sin
    descripcion compacta definida conserva la original.
    """
    if version != "v2":
        return tools

    esquemas = []
    for tool in tools:
        esquema = convert_to_openai_tool(tool)
        funcion = esquema["function"]
        compacta = DESCRIPCIONES_V2.get(funcion["name"])
        if compacta is None:
            esquemas.append(esquema)
            continue

        descripcion, parametros = compacta
        funcion["description"] = descripcion
        propiedades = funcion.get("parameters", {}).get("properties", {})
        for nombre, propiedad in list(propiedades.items()):
            propiedades[nombre] = _simplificar_propiedad(propiedad, parametros.get(nombre))
        funcion.get("parameters", {}).pop("title", None)
        esquemas.append(esquema)
    return esquemas
