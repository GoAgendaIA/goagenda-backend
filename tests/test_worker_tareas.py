"""
P3: tareas en segundo plano fuera de la API. Un solo calendario (services/tareas_periodicas.py) que ejecuta
la API en modo "api" o que encola el scheduler del worker en modo "worker" (Dramatiq + Redis).
"""

from unittest.mock import MagicMock

import pytest

from core import settings
from services import tareas_periodicas as tp

NOMBRES = {"recordatorios", "expirar_pagos", "limpiar_conversaciones"}


# --- el calendario ---------------------------------------------------------------------------------------------

def test_el_calendario_tiene_las_tres_tareas_con_su_frecuencia():
    por_nombre = {t.nombre: t for t in tp.TAREAS}
    assert set(por_nombre) == NOMBRES and len(tp.TAREAS) == 3
    assert (por_nombre["recordatorios"].disparador, por_nombre["recordatorios"].argumentos) == ("interval", {"minutes": 5})
    assert (por_nombre["expirar_pagos"].disparador, por_nombre["expirar_pagos"].argumentos) == ("interval", {"minutes": 15})
    limpieza = por_nombre["limpiar_conversaciones"]
    assert limpieza.disparador == "cron" and limpieza.argumentos["misfire_grace_time"] == 3600


def test_cada_tarea_resuelve_a_su_funcion_real():
    from services.reminder_service import revisar_y_enviar_recordatorios
    from services.retencion_conversaciones import limpiar_conversaciones_vencidas
    from services.wompi_payment_requests import expirar_solicitudes_vencidas

    funciones = {t.nombre: t.callable() for t in tp.TAREAS}
    assert funciones == {
        "recordatorios": revisar_y_enviar_recordatorios,
        "expirar_pagos": expirar_solicitudes_vencidas,
        "limpiar_conversaciones": limpiar_conversaciones_vencidas,
    }


def test_registrar_agrega_cada_tarea_con_id_y_lo_que_devuelve_ejecutar():
    scheduler = MagicMock()
    registrar_a = lambda tarea: f"correr-{tarea.nombre}"  # noqa: E731
    tp.registrar(scheduler, registrar_a)

    llamadas = {c.kwargs["id"]: c for c in scheduler.add_job.call_args_list}
    assert set(llamadas) == NOMBRES
    assert llamadas["recordatorios"].args == ("correr-recordatorios", "interval")
    assert llamadas["recordatorios"].kwargs["minutes"] == 5 and llamadas["recordatorios"].kwargs["replace_existing"] is True


# --- modo "api" y modo "worker" ------------------------------------------------------------------------------------

def test_en_modo_api_la_api_programa_las_tareas_y_arranca_el_scheduler():
    scheduler = MagicMock()
    assert tp.iniciar_en_la_api(scheduler, "api") is True
    assert scheduler.add_job.call_count == 3 and scheduler.start.called


def test_en_modo_worker_la_api_no_programa_nada():
    """Si la API tambien programara, cada recordatorio saldria dos veces (API + worker)."""
    scheduler = MagicMock()
    assert tp.iniciar_en_la_api(scheduler, "worker") is False
    scheduler.add_job.assert_not_called()
    scheduler.start.assert_not_called()


@pytest.mark.parametrize("valor, modo", [(None, "api"), ("", "api"), ("api", "api"), ("worker", "worker"), (" WORKER ", "worker"),
                                         ("otra-cosa", "api")])
def test_el_modo_se_lee_del_entorno_con_api_por_defecto(valor, modo):
    assert settings._modo_de_tareas(valor) == modo


def test_por_defecto_la_api_conserva_el_comportamiento_de_siempre():
    """Sin configurar nada, desplegar este cambio no mueve las tareas de lugar."""
    assert settings.TAREAS_EN_SEGUNDO_PLANO == "api"


# --- el worker (Dramatiq) --------------------------------------------------------------------------------------------

def test_el_worker_exige_redis_url():
    import worker

    with pytest.raises(RuntimeError, match="REDIS_URL"):
        worker.crear_broker(None)
    with pytest.raises(RuntimeError):
        worker.crear_broker("")


def test_el_worker_crea_el_broker_de_redis_con_su_espacio_de_nombres():
    import worker

    broker = worker.crear_broker("redis://redis.test:6379/3")
    assert type(broker).__name__ == "RedisBroker" and broker.namespace == "goagenda"


def test_hay_un_actor_por_tarea_con_reintentos_acotados():
    import worker

    assert set(worker.ACTORES) == NOMBRES
    for nombre, actor in worker.ACTORES.items():
        assert actor.actor_name == f"tarea_{nombre}"
        assert actor.queue_name == "tareas"
        assert actor.options["max_retries"] == 3 and actor.options["min_backoff"] >= 30_000


def test_un_actor_ejecuta_la_funcion_real_de_su_tarea(monkeypatch):
    import worker
    from services import reminder_service

    llamadas = []
    monkeypatch.setattr(reminder_service, "revisar_y_enviar_recordatorios", lambda: llamadas.append("ok") or 7)
    assert worker.ACTORES["recordatorios"].fn() == 7 and llamadas == ["ok"]


def test_el_worker_y_el_scheduler_no_cargan_el_agente_ni_el_pool_de_postgres():
    """
    Importar el agente abre un pool de Postgres (el pooler de Supabase admite 15 conexiones en total entre
    todos los procesos). Se comprueba en un proceso limpio, sin los modulos que ya cargaron otras pruebas.
    """
    import subprocess
    import sys

    codigo = (
        "import sys, worker, scheduler\n"
        "scheduler.crear_scheduler()\n"
        "from services.tareas_periodicas import TAREAS\n"
        "[t.callable() for t in TAREAS if t.nombre != 'limpiar_conversaciones']\n"
        "print('agent' in {m.split('.')[0] for m in sys.modules}, 'psycopg_pool' in sys.modules)\n"
    )
    entorno = {"PATH": "/usr/bin:/bin", "DATABASE_URL": "postgresql://u:p@h:5432/d", "REDIS_URL": "redis://r:6379/0",
               "SUPABASE_URL": "https://abc.supabase.co", "SUPABASE_KEY": "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.x"}
    resultado = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, env=entorno, timeout=60,
                               cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    assert resultado.returncode == 0, resultado.stderr[-500:]
    assert resultado.stdout.strip().splitlines()[-1] == "False False"


# --- el scheduler del worker --------------------------------------------------------------------------------------------

def test_el_scheduler_del_worker_programa_las_tres_tareas():
    import scheduler as sch

    assert {t.id for t in sch.crear_scheduler().get_jobs()} == NOMBRES


def test_ejecutar_un_trabajo_del_scheduler_encola_en_dramatiq(monkeypatch):
    import scheduler as sch
    import worker

    encolado = []
    for nombre, actor in worker.ACTORES.items():
        monkeypatch.setattr(actor, "send", lambda n=nombre: encolado.append(n))

    scheduler = sch.crear_scheduler()
    for trabajo in scheduler.get_jobs():
        trabajo.func()
    assert sorted(encolado) == sorted(NOMBRES)


def test_el_scheduler_usa_utc_para_el_cron_de_la_limpieza():
    import scheduler as sch

    assert str(sch.crear_scheduler().timezone) == "UTC"


# --- start_worker.sh ------------------------------------------------------------------------------------------------------

def _correr_start_worker(tmp_path, scheduler_sh, dramatiq_sh, senal=None, espera=6):
    """Ejecuta el script real con `python` y `dramatiq` de mentira (cada uno, un shell script)."""
    import os
    import pathlib
    import signal
    import subprocess
    import time

    raiz = pathlib.Path(__file__).resolve().parent.parent
    (tmp_path / "python").write_text(f"#!/bin/bash\n{scheduler_sh}\n")
    (tmp_path / "dramatiq").write_text(f"#!/bin/bash\n{dramatiq_sh}\n")
    for f in ("python", "dramatiq"):
        os.chmod(tmp_path / f, 0o755)
    proceso = subprocess.Popen(["/bin/bash", str(raiz / "start_worker.sh")], cwd=tmp_path, stderr=subprocess.PIPE, text=True,
                               env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    if senal:
        time.sleep(1.5)
        proceso.send_signal(senal)
    try:
        proceso.wait(timeout=espera)
    except subprocess.TimeoutExpired:
        proceso.kill()
        raise
    return proceso.returncode, proceso.stderr.read()


def test_si_el_scheduler_muere_el_contenedor_termina_con_error_y_apaga_el_worker(tmp_path):
    codigo, error = _correr_start_worker(tmp_path, "exit 3", "exec sleep 60")
    assert codigo == 3 and "scheduler termino" in error


def test_si_el_worker_muere_el_contenedor_termina_con_error(tmp_path):
    codigo, error = _correr_start_worker(tmp_path, "exec sleep 60", "exit 0")
    assert codigo == 1 and "worker termino" in error   # un 0 inesperado tambien es una falla: debe reiniciarse


def test_un_apagado_ordenado_no_es_una_falla(tmp_path):
    import signal

    codigo, _ = _correr_start_worker(tmp_path, "exec sleep 60", "exec sleep 60", senal=signal.SIGTERM)
    assert codigo == 0
