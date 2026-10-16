"""
Bug 2: las conversaciones del chat se eliminan a los 3 dias de su ultima actividad, una por
una, sin tocar citas, reservas ni las metricas.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from agent import graph as g
from services import retencion_conversaciones as rc
from tests.test_fase3 import BIZ, enviar, hora_fija, modelos, negocio, texto  # noqa: F401

AHORA = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def hilo(sid):
    return f"{BIZ}:{sid}"


def tiene_historial(sid) -> bool:
    return bool(g.GRAPH.get_state(g._thread_config(BIZ, sid)).values)


@pytest.fixture
def dos_conversaciones(negocio, modelos):
    """Dos clientes distintos conversando: A y B."""
    modelos.principal.respuestas = [texto("Hola A, ¿qué servicio?"), texto("Hola B, ¿qué servicio?")]
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    enviar("quiero una cita", a)
    enviar("necesito un turno", b)
    return a, b


def fijar_actividad(monkeypatch, **por_hilo):
    monkeypatch.setattr(rc, "ultima_actividad_por_hilo", lambda: por_hilo)


# --- la regla de los 3 dias, por conversacion ---------------------------------------------------------------

def test_cada_conversacion_tiene_su_propio_plazo(dos_conversaciones, monkeypatch):
    a, b = dos_conversaciones
    fijar_actividad(monkeypatch, **{hilo(a): AHORA - timedelta(days=4), hilo(b): AHORA - timedelta(days=1)})

    r = rc.limpiar_conversaciones_vencidas(ahora=AHORA)

    assert r == {"activa": True, "retencion_dias": 3, "dry_run": False, "conversaciones": 2,
                 "vencidas": 1, "eliminadas": 1, "fallidas": 0}
    assert not tiene_historial(a)       # A cumplio los 3 dias: eliminada
    assert tiene_historial(b)           # B aun no: intacta


def test_justo_en_el_limite_todavia_no_se_elimina(dos_conversaciones, monkeypatch):
    a, b = dos_conversaciones
    fijar_actividad(monkeypatch, **{hilo(a): AHORA - timedelta(days=3), hilo(b): AHORA - timedelta(days=3, seconds=1)})
    r = rc.limpiar_conversaciones_vencidas(ahora=AHORA)
    assert r["vencidas"] == 1 and tiene_historial(a) and not tiene_historial(b)


def test_la_retencion_se_cuenta_desde_la_ultima_actividad_real(dos_conversaciones):
    """Con checkpoints reales: la ultima actividad es la del checkpoint mas reciente de cada hilo."""
    a, b = dos_conversaciones
    ultimos = rc.ultima_actividad_por_hilo()
    assert set(ultimos) >= {hilo(a), hilo(b)}
    ahora = datetime.now(timezone.utc)
    assert all(abs((ahora - momento).total_seconds()) < 60 for momento in ultimos.values())

    # Nada es viejo todavia, y pasados 4 dias sin movimiento todo vence.
    assert tiene_historial(a) and tiene_historial(b)
    r = rc.limpiar_conversaciones_vencidas(ahora=ahora + timedelta(days=4))
    # El checkpointer en memoria es compartido entre pruebas: puede traer hilos de otras.
    assert r["eliminadas"] >= 2 and not tiene_historial(a) and not tiene_historial(b)


def test_despues_de_eliminada_la_conversacion_puede_empezar_de_cero(dos_conversaciones, modelos):
    a, _ = dos_conversaciones
    rc.limpiar_conversaciones_vencidas(ahora=datetime.now(timezone.utc) + timedelta(days=4))
    historial, _ = g.obtener_historial(BIZ, a)
    assert historial == []

    modelos.principal.respuestas = [texto("¡Hola de nuevo! ¿Qué servicio quieres?")]
    assert enviar("hola", a)[0] == "¡Hola de nuevo! ¿Qué servicio quieres?"


# --- lo que NO se debe tocar ------------------------------------------------------------------------------------

def test_no_borra_citas_ni_metricas_ni_otros_registros(dos_conversaciones, db, monkeypatch):
    a, b = dos_conversaciones
    db.sembrar("appointments", {"id": "c1", "business_id": BIZ, "client_phone": "573001234567", "status": "confirmed",
                                "scheduled_at": "2026-10-20T10:00:00"})
    db.sembrar("businesses", {"id": "otro", "name": "Otro"})
    sesiones_antes = sorted(s["session_id"] for s in db.filas("chat_sessions"))  # las crea el seguimiento de metricas
    assert sesiones_antes == sorted([a, b])
    fijar_actividad(monkeypatch, **{hilo(a): AHORA - timedelta(days=30), hilo(b): AHORA - timedelta(days=30)})

    rc.limpiar_conversaciones_vencidas(ahora=AHORA)

    assert [c["id"] for c in db.filas("appointments")] == ["c1"]
    assert sorted(s["session_id"] for s in db.filas("chat_sessions")) == sesiones_antes
    assert len(db.filas("businesses")) == 2


def test_ensayo_no_borra_nada(dos_conversaciones, monkeypatch):
    a, b = dos_conversaciones
    fijar_actividad(monkeypatch, **{hilo(a): AHORA - timedelta(days=10)})
    r = rc.limpiar_conversaciones_vencidas(ahora=AHORA, dry_run=True)
    assert r["dry_run"] is True and r["vencidas"] == 1 and r["eliminadas"] == 0
    assert tiene_historial(a)


@pytest.mark.parametrize("dias", [0, -1])
def test_con_retencion_en_cero_la_limpieza_esta_desactivada(dos_conversaciones, monkeypatch, dias):
    a, _ = dos_conversaciones
    fijar_actividad(monkeypatch, **{hilo(a): AHORA - timedelta(days=400)})
    r = rc.limpiar_conversaciones_vencidas(dias=dias, ahora=AHORA)
    assert r["activa"] is False and r["eliminadas"] == 0 and tiene_historial(a)


def test_usa_el_plazo_configurado_por_defecto(dos_conversaciones, monkeypatch):
    a, _ = dos_conversaciones
    monkeypatch.setattr(rc, "CHAT_RETENCION_DIAS", 10)
    fijar_actividad(monkeypatch, **{hilo(a): AHORA - timedelta(days=5)})
    assert rc.limpiar_conversaciones_vencidas(ahora=AHORA)["vencidas"] == 0
    assert rc.limpiar_conversaciones_vencidas(dias=4, ahora=AHORA)["vencidas"] == 1


def test_una_falla_al_borrar_no_detiene_las_demas(dos_conversaciones, monkeypatch, capsys):
    a, b = dos_conversaciones
    fijar_actividad(monkeypatch, **{hilo(a): AHORA - timedelta(days=9), hilo(b): AHORA - timedelta(days=9)})
    original = rc.eliminar_hilo
    primero = sorted([hilo(a), hilo(b)])[0]  # el primero en orden es el que falla

    def a_veces_falla(thread_id):
        if thread_id == primero:
            raise RuntimeError("base caida")
        original(thread_id)

    monkeypatch.setattr(rc, "eliminar_hilo", a_veces_falla)
    r = rc.limpiar_conversaciones_vencidas(ahora=AHORA)
    assert (r["vencidas"], r["eliminadas"], r["fallidas"]) == (2, 1, 1)
    assert "no se pudo eliminar" in capsys.readouterr().out


# --- Postgres (agregacion en la base) y utilidades --------------------------------------------------------------

def test_en_postgres_la_ultima_actividad_se_agrega_en_la_base(monkeypatch):
    consultas = []

    class Resultado:
        def fetchall(self):
            return [
                {"thread_id": "b:1", "ultima_actividad": datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)},
                {"thread_id": "b:2", "ultima_actividad": datetime(2026, 10, 2, 8, 0)},  # sin zona: se asume UTC
                {"thread_id": "b:3", "ultima_actividad": None},                         # sin fecha: se ignora
            ]

    class Conexion:
        def execute(self, sql):
            consultas.append(sql)
            return Resultado()

    class Pool:
        @contextmanager
        def connection(self):
            yield Conexion()

    monkeypatch.setattr(rc, "_es_postgres", lambda c: True)
    monkeypatch.setattr(g, "_pool", Pool())

    ultimos = rc.ultima_actividad_por_hilo()
    assert ultimos == {"b:1": datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc),
                       "b:2": datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)}
    assert "GROUP BY thread_id" in consultas[0] and "checkpoint->>'ts'" in consultas[0]


def test_es_postgres_distingue_el_checkpointer():
    class FalsoPg:
        pass

    FalsoPg.__module__ = "langgraph.checkpoint.postgres"
    assert rc._es_postgres(FalsoPg()) and not rc._es_postgres(object())


@pytest.mark.parametrize("valor, esperado", [
    ("2026-10-01T08:00:00Z", datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)),
    ("2026-10-01T08:00:00+00:00", datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)),
    ("2026-10-01T03:00:00-05:00", datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)),
    (datetime(2026, 10, 1, 8, 0), datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)),
    ("no es fecha", None), (None, None), (12345, None),
])
def test_a_utc(valor, esperado):
    assert rc._a_utc(valor) == esperado


# --- programacion y endpoint de verificacion ------------------------------------------------------------------------

def test_el_job_diario_esta_registrado_con_cron():
    import main

    jobs = {job[0]: (job[1], job[2]) for job in main.scheduler.jobs}
    argumentos, opciones = jobs[rc.limpiar_conversaciones_vencidas]
    assert argumentos == ("cron",) and opciones == {"hour": 8, "minute": 0, "misfire_grace_time": 3600}


def test_endpoint_de_super_admin_ensaya_por_defecto_y_borra_si_se_pide(cliente, como, db, dos_conversaciones, monkeypatch):
    a, _ = dos_conversaciones
    db.sembrar("super_admins", {"user_id": "root"})
    fijar_actividad(monkeypatch, **{hilo(a): datetime.now(timezone.utc) - timedelta(days=9)})

    assert cliente.post("/admin/chat-retention/run").status_code == 401
    assert cliente.post("/admin/chat-retention/run", headers=como("normal")).status_code == 403

    ensayo = cliente.post("/admin/chat-retention/run", headers=como("root")).json()
    assert ensayo["dry_run"] is True and ensayo["vencidas"] == 1 and ensayo["eliminadas"] == 0
    assert tiene_historial(a)

    real = cliente.post("/admin/chat-retention/run?dry_run=false", headers=como("root")).json()
    assert real["dry_run"] is False and real["eliminadas"] == 1
    assert not tiene_historial(a)
