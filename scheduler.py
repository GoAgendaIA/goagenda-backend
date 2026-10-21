"""
Disparador periodico del worker: cada tarea de services/tareas_periodicas.py se ENCOLA en Dramatiq; no se
ejecuta aqui. Vive en el mismo contenedor que el worker (ver start_worker.sh).

UNA sola instancia: dos schedulers encolarian cada tarea dos veces (recordatorios duplicados).

    python scheduler.py
"""

from apscheduler.schedulers.blocking import BlockingScheduler

from services.tareas_periodicas import registrar


def crear_scheduler() -> BlockingScheduler:
    from worker import ACTORES

    scheduler = BlockingScheduler(timezone="UTC")
    registrar(scheduler, lambda tarea: ACTORES[tarea.nombre].send)
    return scheduler


if __name__ == "__main__":
    scheduler = crear_scheduler()
    print("[scheduler] encolando tareas:", ", ".join(job.id for job in scheduler.get_jobs()), flush=True)
    scheduler.start()
