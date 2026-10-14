"""
Aislamiento de las pruebas. IMPORTANTE: el .env local apunta a PRODUCCION
(Supabase, OpenAI, WhatsApp, Wompi), e importar ciertos modulos tiene
efectos reales (agent/graph.py se conecta a Postgres; main.py escribe el
super admin en Supabase y arranca un scheduler que manda recordatorios por
WhatsApp). Todo lo de este archivo corre ANTES de que cualquier prueba
importe codigo de la aplicacion, y garantiza que ninguna prueba toque nada
real:

1. load_dotenv queda desactivado y todas las variables son falsas.
2. La red esta bloqueada: cualquier conexion de socket lanza un error (si a
   una prueba le falta un mock, falla en vez de llegar a produccion).
3. Supabase es un doble en memoria (tests/fakes.py).
4. Postgres (checkpointer del agente) es un checkpointer en memoria.
5. El scheduler de APScheduler no arranca nada.
"""

import os
import socket
import sys
from pathlib import Path

import pytest

# --- 1. Sin .env real -------------------------------------------------------
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False
dotenv.main.load_dotenv = dotenv.load_dotenv

from cryptography.fernet import Fernet  # noqa: E402

_ENV_DE_PRUEBA = {
    "SUPABASE_URL": "http://supabase.test",
    "SUPABASE_KEY": "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.test-anon",
    "SUPABASE_SERVICE_ROLE_KEY": "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZSJ9.test-service",
    "DATABASE_URL": "postgresql://test:test@db.test:5432/test",
    "OPENAI_API_KEY": "sk-test",
    "CHAT_MODEL_NAME": "gpt-test",
    "WHATSAPP_TOKEN": "wa-test-token",
    "VERIFY_TOKEN": "verify-test",
    "GOAGENDA_WHATSAPP_TOKEN": "goagenda-wa-test",
    "GOAGENDA_WHATSAPP_PHONE_NUMBER_ID": "111111111",
    "BAILEYS_INTERNAL_API_KEY": "internal-test-key",
    "BAILEYS_SERVICE_URL": "http://baileys.test",
    "WOMPI_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "GOAGENDA_FRONTEND_URL": "http://frontend.test",
    "CORS_ALLOWED_ORIGINS": "http://frontend.test",
    "SUPER_ADMIN_EMAIL": "",
    "SUPER_ADMIN_PASSWORD": "",
    "FIREBASE_CREDENTIALS_JSON": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
}
for _clave in list(os.environ):
    if _clave.startswith(("SUPABASE", "WOMPI", "WHATSAPP", "GOAGENDA", "OPENAI", "FIREBASE", "BAILEYS", "DB_POOL")):
        del os.environ[_clave]
os.environ.update(_ENV_DE_PRUEBA)

# Evaluacion en vivo (evaluacion/README.md): UNICO caso en que el modelo real es
# alcanzable. Exige EVAL_LIVE=1 y una clave propia de la evaluacion
# (EVAL_OPENAI_API_KEY), nunca la de produccion. Todo lo demas sigue bloqueado.
EVAL_EN_VIVO = os.environ.get("EVAL_LIVE") == "1"
if EVAL_EN_VIVO:
    os.environ["OPENAI_API_KEY"] = os.environ["EVAL_OPENAI_API_KEY"]
    os.environ["CHAT_MODEL_NAME"] = os.environ.get("EVAL_MODELO") or "gpt-4.1-mini"

# --- 2. Red bloqueada -------------------------------------------------------
class RedBloqueadaEnPruebas(RuntimeError):
    pass


def _sin_red(self, direccion, *args, **kwargs):
    raise RedBloqueadaEnPruebas(f"Las pruebas no pueden abrir conexiones de red (intento a {direccion}). Falta un mock.")


if EVAL_EN_VIVO:
    # Se bloquea la RESOLUCION DE NOMBRES: solo api.openai.com puede resolverse.
    _getaddrinfo_original = socket.getaddrinfo

    def _solo_openai(host, *args, **kwargs):
        if host in ("api.openai.com", "localhost", "127.0.0.1"):
            return _getaddrinfo_original(host, *args, **kwargs)
        raise RedBloqueadaEnPruebas(f"La evaluacion en vivo solo puede alcanzar api.openai.com (intento a {host}).")

    socket.getaddrinfo = _solo_openai
else:
    socket.socket.connect = _sin_red
    socket.socket.connect_ex = _sin_red

# --- 3. Supabase en memoria ---------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.fakes import FakeSupabase  # noqa: E402

FAKE_SUPABASE = FakeSupabase()
import supabase as _supabase_pkg  # noqa: E402

_supabase_pkg.create_client = lambda *args, **kwargs: FAKE_SUPABASE

# --- 4. Postgres del checkpointer -> memoria ---------------------------------
import psycopg_pool  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
import langgraph.checkpoint.postgres as _lg_postgres  # noqa: E402


class _PoolFalso:
    def __init__(self, *args, **kwargs):
        self.min_size = kwargs.get("min_size")
        self.max_size = kwargs.get("max_size")
        self.timeout = kwargs.get("timeout")
        self.kwargs = kwargs

    @staticmethod
    def check_connection(conn):
        return None

    def connection(self, *args, **kwargs):
        raise RedBloqueadaEnPruebas("El pool de Postgres no esta disponible en pruebas")

    def close(self):
        pass


class _CheckpointerEnMemoria(InMemorySaver):
    def __init__(self, conn=None, *args, **kwargs):
        super().__init__()

    def setup(self):
        pass


psycopg_pool.ConnectionPool = _PoolFalso
_lg_postgres.PostgresSaver = _CheckpointerEnMemoria

# --- 5. Scheduler sin efectos -----------------------------------------------
import apscheduler.schedulers.background as _aps  # noqa: E402


class _SchedulerFalso:
    def __init__(self, *args, **kwargs):
        self.jobs = []

    def add_job(self, func, *args, **kwargs):
        self.jobs.append((func, args, kwargs))

    def start(self):
        pass

    def shutdown(self, *args, **kwargs):
        pass


_aps.BackgroundScheduler = _SchedulerFalso


# --- fixtures -----------------------------------------------------------------
@pytest.fixture(autouse=True)
def db():
    """Supabase en memoria, vacio al empezar cada prueba."""
    FAKE_SUPABASE.reset()
    yield FAKE_SUPABASE
    FAKE_SUPABASE.reset()


@pytest.fixture(autouse=True)
def _sin_cache_de_geocodificacion():
    from services import geocoding

    geocoding._cache.clear()
    yield
    geocoding._cache.clear()


@pytest.fixture(autouse=True)
def _sin_estado_de_salvaguardas():
    """El limitador de peticiones y el cache de presupuesto viven en memoria: no deben filtrarse entre pruebas."""
    from services import ai_usage_tracking, rate_limit

    rate_limit.reiniciar()
    ai_usage_tracking.limpiar_cache_presupuesto()
    yield
    rate_limit.reiniciar()
    ai_usage_tracking.limpiar_cache_presupuesto()


@pytest.fixture
def cliente():
    """TestClient sobre la app REAL de main.py (seguro de importar gracias al aislamiento de arriba)."""
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as cliente_http:
        yield cliente_http


@pytest.fixture
def como(db):
    """como("user-1") -> headers con un Bearer token que la dependencia real de auth resuelve a ese usuario."""

    def _headers(user_id: str) -> dict:
        token = f"tok-{user_id}"
        db.auth.usuarios_por_token[token] = user_id
        return {"Authorization": f"Bearer {token}"}

    return _headers
