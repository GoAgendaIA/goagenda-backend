"""
Atajos que evitan llamadas al modelo (T3.1) y enrutamiento de modelo (T3.2).

Cada ida y vuelta con el modelo reenvia todo el contexto; los pasos cuyo
resultado ya esta determinado por los datos (un saludo, un resumen que la
herramienta ya armo) no necesitan al modelo. Todo esta detras de banderas y
apagado por defecto: se activa tras compararlo con la evaluacion en vivo
(evaluacion/README.md).

CHAT_FLUJOS_DETERMINISTAS: lista separada por comas o "all":
  - saludo: un saludo puro al inicio se responde en codigo con la lista real.
  - confirmacion: el resumen de pedir_confirmacion_cita se entrega tal cual.
  - servicios: ante una pregunta obvia por servicios/precios la primera
    llamada ya es la consulta (en vez de generar texto que luego se descarta).

CHAT_ENRUTAMIENTO=relay (con CHAT_MODEL_NAME_SIMPLE): las llamadas que solo
tienen que relatar el resultado de una consulta usan el modelo barato.
"""

import re
import unicodedata

FLUJOS_DISPONIBLES = ("saludo", "confirmacion", "servicios")

# Herramientas cuyo resultado el modelo solo tiene que relatar al cliente: no
# deciden nada ni manejan dinero. crear_cita (link de pago exacto), cancelar,
# reprogramar y las de escalamiento quedan SIEMPRE con el modelo principal.
TOOLS_DE_RELEVO_SEGURAS = {
    "consultar_servicios_disponibles",
    "consultar_horas_disponibles",
    "consultar_empleados_disponibles",
    "consultar_zonas_domicilio",
    "registrar_telefono_cliente",
    "seleccionar_empleado",
    "seleccionar_servicio",
    "consultar_citas_cliente",
}


def flujos_activos(valor: str | None) -> frozenset[str]:
    """'all'/'on' -> todos; '' -> ninguno; 'saludo,servicios' -> esos (los desconocidos se ignoran)."""
    texto = (valor or "").strip().lower()
    if texto in ("all", "on", "1", "true", "si"):
        return frozenset(FLUJOS_DISPONIBLES)
    return frozenset(p.strip() for p in texto.split(",") if p.strip() in FLUJOS_DISPONIBLES)


def _normalizar(texto: str) -> str:
    sin_tildes = "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", sin_tildes.lower()).strip()


_SALUDO = re.compile(
    r"^[¡¿!?\s]*(?:(?:hola|holi|holaa+|buenas|buen dia|buenos dias|buenas tardes|buenas noches|hey|ey|saludos|hello|hi|"
    r"buenas buenas)(?: (?:que tal|como estas|como estan|como va|a todos|a todas|equipo))?)[\s!¡?¿.,😊👋🙂]*$"
)


def es_saludo_puro(texto: str | None) -> bool:
    """True solo si el mensaje es un saludo y nada mas ('hola', 'buenas tardes!')."""
    if not texto:
        return False
    return bool(_SALUDO.match(_normalizar(str(texto))))


_PREGUNTA_SERVICIOS = re.compile(
    r"(?:que|cuales|cuantos) (?:servicios|tratamientos|cortes)\b"
    r"|\b(?:servicios|precios|tarifas|catalogo|lista de (?:servicios|precios))\b"
    r"|\bcuanto (?:cuesta|vale|cobran|sale|es)\b"
    r"|\bque (?:hacen|ofrecen|manejan)\b"
)
# Si el mensaje ya trae una accion, no es solo una pregunta de servicios: lo decide el modelo.
_ACCION = re.compile(r"\b(?:agendar|reservar|cancelar|cambiar|mover|reprogramar|cita|citas|turno|hablar)\b")


def pregunta_por_servicios(texto: str | None) -> bool:
    """True si el cliente solo pregunta que servicios hay o cuanto cuestan (sin pedir agendar ni gestionar)."""
    if not texto:
        return False
    normal = _normalizar(str(texto))
    return bool(_PREGUNTA_SERVICIOS.search(normal)) and not _ACCION.search(normal)


def emoji_del_negocio(tipo: str | None) -> str:
    t = _normalizar(tipo or "")
    if "barber" in t:
        return "💈"
    if "spa" in t or "masaj" in t:
        return "✨"
    if "una" in t or "nail" in t or "manicur" in t:
        return "💅"
    return "💇"
