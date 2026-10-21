"""
Worker de tareas en segundo plano (Dramatiq + Redis).

    dramatiq worker --processes 1 --threads 4        (o: sh start_worker.sh, que ademas lanza scheduler.py)

Ejecuta las tareas que `scheduler.py` encola: recordatorios de WhatsApp, expiracion de pagos de Wompi y
retencion de conversaciones. NO sirve HTTP.

UNA sola instancia, UN solo proceso: es lo que impide los recordatorios duplicados (--processes 1).
Las tareas son idempotentes (un recordatorio enviado queda marcado; un pago expirado ya no esta
pendiente), asi que un reintento tras una falla a medias no repite el efecto.
"""

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from core.settings import REDIS_URL
from services.tareas_periodicas import TAREAS


def crear_broker(url: str | None) -> RedisBroker:
    if not url:
        raise RuntimeError(
            "Falta REDIS_URL: el worker necesita el broker de Dramatiq (ej. redis://redis:6379/0 o la URL rediss:// de Redis Cloud)."
        )
    return RedisBroker(url=url, namespace="goagenda")


dramatiq.set_broker(crear_broker(REDIS_URL))

# Reintentos pocos y espaciados (30 s minimo): una falla de WhatsApp o de Supabase suele ser transitoria y
# el siguiente ciclo del scheduler vuelve a intentar de todos modos.
_OPCIONES = dict(queue_name="tareas", max_retries=3, min_backoff=30_000, max_backoff=300_000, time_limit=10 * 60 * 1000)


def _definir_actor(tarea):
    def ejecutar():
        return tarea.callable()()

    ejecutar.__name__ = f"tarea_{tarea.nombre}"
    return dramatiq.actor(**_OPCIONES)(ejecutar)


# Un actor por tarea, por nombre: ACTORES["recordatorios"].send() la encola.
ACTORES = {tarea.nombre: _definir_actor(tarea) for tarea in TAREAS}
