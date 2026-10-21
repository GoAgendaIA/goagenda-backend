import os

from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CHAT_MODEL_NAME = os.getenv("CHAT_MODEL_NAME") or "gpt-4.1-mini"

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "Falta DATABASE_URL: la cadena de conexion directa a Postgres de Supabase "
        "(Project Settings > Database > Connection string, puerto 5432, no el "
        "transaction pooler de 6543). La usa el checkpointer del agente de chat."
    )

# Pool de conexiones del checkpointer (agent/graph.py). El pooler de
# Supabase en modo sesion admite solo 15 clientes EN TOTAL, compartidos
# por todos los procesos conectados a la misma base (produccion, un
# uvicorn local apuntando a la misma base, scripts...). Con el default de
# psycopg_pool (4 a 10 conexiones por proceso) dos procesos bastaban para
# agotarlo, y el chat respondia 500 ("No se pudo enviar el mensaje"). Las
# operaciones del checkpointer son cortas, asi que pocas conexiones
# alcanzan.
DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE") or 1)
DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE") or 4)
# Segundos que una peticion espera por una conexion libre antes de fallar
# (default de psycopg_pool: 30s, que se sumaba a la espera del cliente).
DB_POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT") or 10)


# ---------------------------------------------------------------------
# Salvaguardas de costo del chat publico (ver docs/ESTRATEGIA_OPTIMIZACION_TOKENS.md).
# El chat es publico por diseño: cualquiera puede mandarle mensajes, y cada
# mensaje cuesta tokens de OpenAI. Todos son ajustables por variable de entorno.
# ---------------------------------------------------------------------
def _entero_env(nombre: str, por_defecto: int) -> int:
    try:
        return int(os.getenv(nombre) or por_defecto)
    except ValueError:
        return por_defecto


# Largo maximo de un mensaje del cliente: un mensaje enorme se paga completo
# en cada llamada del turno (y en las siguientes, porque queda en el historial).
CHAT_MENSAJE_MAX_CARACTERES = _entero_env("CHAT_MENSAJE_MAX_CARACTERES", 600)
# Ventana de 60 s por sesion (una persona real escribe a esa velocidad) y por IP.
CHAT_LIMITE_SESION_POR_MINUTO = _entero_env("CHAT_LIMITE_SESION_POR_MINUTO", 12)
CHAT_LIMITE_IP_POR_MINUTO = _entero_env("CHAT_LIMITE_IP_POR_MINUTO", 40)
# Tope de mensajes por sesion en una hora: corta conversaciones sin fin.
CHAT_LIMITE_SESION_POR_HORA = _entero_env("CHAT_LIMITE_SESION_POR_HORA", 80)
# Tope de tokens de la RESPUESTA del modelo (los argumentos de las tools tambien cuentan).
LLM_MAX_TOKENS_SALIDA = _entero_env("LLM_MAX_TOKENS_SALIDA", 600)
# Limite de tiempo de CADA llamada al modelo. Sin el, una llamada lenta de OpenAI esperaba
# indefinidamente y el tunel de Cloudflare cortaba la peticion a los ~100 s (504): el cliente veia
# "no se pudo enviar" y su turno quedaba sin respuesta. Con 25 s y los 3 intentos de
# _invocar_modelo, el peor caso (~80 s) queda por debajo del corte y el cliente recibe al menos el
# mensaje de error tecnico con el WhatsApp del negocio.
LLM_TIMEOUT_SEGUNDOS = _entero_env("LLM_TIMEOUT_SEGUNDOS", 25)
# Corte duro del presupuesto mensual de IA por negocio. Por defecto APAGADO:
# el presupuesto por defecto es de US$5 y activarlo sin medir antes podria
# dejar sin bot a un negocio real. Activar despues de fijar la linea base (T0.1).
AI_PRESUPUESTO_CORTE_DURO = (os.getenv("AI_PRESUPUESTO_CORTE_DURO") or "").strip().lower() in ("1", "true", "yes", "si")

# Cuantos turnos del cliente (mensajes humanos) se envian al modelo. El historial
# completo sigue guardado: esto solo acota lo que viaja en cada llamada, cuyo
# costo crecia con la longitud de la conversacion (T1.2).
CHAT_HISTORIAL_MAX_TURNOS = _entero_env("CHAT_HISTORIAL_MAX_TURNOS", 12)

# Versiones del contexto que se envia al modelo (T2.2 y T2.3). "v1" es el
# comportamiento actual; "v2" es la version compacta. Se mantienen detras de una
# bandera, apagadas por defecto, hasta que la evaluacion en vivo (evaluacion/)
# confirme que no degradan la calidad.
CHAT_PROMPT_VERSION = (os.getenv("CHAT_PROMPT_VERSION") or "v1").strip().lower()
CHAT_TOOLS_VERSION = (os.getenv("CHAT_TOOLS_VERSION") or "v1").strip().lower()

# --- Fase 3: menos llamadas al modelo y enrutamiento (agent/flujos.py). Todo apagado por defecto. ---
# Lista separada por comas (saludo, confirmacion, servicios) o "all".
CHAT_FLUJOS_DETERMINISTAS = (os.getenv("CHAT_FLUJOS_DETERMINISTAS") or "").strip().lower()
# "relay": las llamadas que solo relatan un resultado usan CHAT_MODEL_NAME_SIMPLE.
CHAT_ENRUTAMIENTO = (os.getenv("CHAT_ENRUTAMIENTO") or "").strip().lower()
CHAT_MODEL_NAME_SIMPLE = (os.getenv("CHAT_MODEL_NAME_SIMPLE") or "").strip()


# ---------------------------------------------------------------------
# Retencion de conversaciones del chat publico (services/retencion_conversaciones.py).
# El historial de cada conversacion (checkpoints de LangGraph) guarda nombres y
# telefonos de clientes: se elimina a los CHAT_RETENCION_DIAS dias de su ULTIMA
# actividad, cada conversacion por separado. 0 desactiva la limpieza.
# La frecuencia es independiente del plazo: el job corre una vez al dia a la
# hora UTC indicada (08:00 UTC = 03:00 en Colombia).
# ---------------------------------------------------------------------
CHAT_RETENCION_DIAS = _entero_env("CHAT_RETENCION_DIAS", 3)
CHAT_LIMPIEZA_HORA_UTC = _entero_env("CHAT_LIMPIEZA_HORA_UTC", 8)


# ---------------------------------------------------------------------
# Tasa de cambio para mostrar los costos de IA en pesos colombianos (panel de metricas del
# super admin). OpenAI factura en USD; esta tasa es CONFIGURABLE y no se consulta a ningun
# servicio externo: cada respuesta de metricas incluye la tasa usada (tasa_cop_por_usd) para
# que el calculo sea auditable. Actualizarla cuando la TRM cambie de forma relevante.
# ---------------------------------------------------------------------
def _tasa_cop_por_usd() -> float:
    try:
        tasa = float(os.getenv("COP_POR_USD") or 4000)
    except ValueError:
        return 4000.0
    return tasa if tasa > 0 else 4000.0


COP_POR_USD = _tasa_cop_por_usd()


# ---------------------------------------------------------------------
# Tareas en segundo plano (recordatorios, expiracion de pagos, retencion de conversaciones).
#
# "api" (por defecto): las programa un APScheduler DENTRO del proceso de la API, como siempre. Sirve
#     mientras haya UNA sola instancia de la API.
# "worker": la API NO programa nada; un proceso aparte (worker.py + scheduler.py, ver docs/WORKER_Y_TAREAS.md)
#     las ejecuta con Dramatiq y Redis. Es lo que se usa cuando API y worker se despliegan por separado.
#
# Con "worker" hay que desplegar el worker, o las tareas dejan de correr. Nunca activar el modo "api" en la
# API y a la vez un worker: los recordatorios saldrian duplicados.
# ---------------------------------------------------------------------
def _modo_de_tareas(valor: str | None) -> str:
    modo = (valor or "api").strip().lower()
    return modo if modo in ("api", "worker") else "api"


TAREAS_EN_SEGUNDO_PLANO = _modo_de_tareas(os.getenv("TAREAS_EN_SEGUNDO_PLANO"))

# Broker de Dramatiq (solo lo usa el worker y el scheduler). Ej.: redis://redis:6379/0 o rediss://... (Redis Cloud).
REDIS_URL = os.getenv("REDIS_URL")
