"""
Las tareas periodicas de GoAgenda y cuando corren: UNA sola definicion para los dos modos de ejecucion.

- Modo "api" (main.py): un APScheduler dentro de la API llama a cada funcion directamente.
- Modo "worker" (scheduler.py): un APScheduler aparte solo ENCOLA cada tarea en Dramatiq, y el worker
  (worker.py) la ejecuta.

Asi el calendario no puede divergir entre ambientes (VPS de pruebas y produccion).
"""

from dataclasses import dataclass, field
from typing import Callable

from core.settings import CHAT_LIMPIEZA_HORA_UTC


@dataclass(frozen=True)
class TareaPeriodica:
    nombre: str
    # Nombre del modulo y de la funcion: se importan al EJECUTAR la tarea, no al definir el
    # calendario. Importar los servicios abre clientes (Supabase y, en la retencion, el pool de
    # Postgres del agente): el scheduler y la API no deben cargarlos de mas.
    modulo: str
    funcion: str
    disparador: str
    argumentos: dict = field(default_factory=dict)

    def callable(self) -> Callable:
        import importlib

        return getattr(importlib.import_module(self.modulo), self.funcion)


TAREAS = (
    TareaPeriodica("recordatorios", "services.reminder_service", "revisar_y_enviar_recordatorios",
                   "interval", {"minutes": 5}),
    TareaPeriodica("expirar_pagos", "services.wompi_payment_requests", "expirar_solicitudes_vencidas",
                   "interval", {"minutes": 15}),
    # Una vez al dia, a una hora fija: con cron y no con un intervalo porque cada despliegue reinicia el
    # proceso y un intervalo de 24 h nunca llegaria a cumplirse; misfire_grace_time lo deja correr si el
    # proceso estaba reiniciando a esa hora.
    TareaPeriodica("limpiar_conversaciones", "services.retencion_conversaciones", "limpiar_conversaciones_vencidas",
                   "cron", {"hour": CHAT_LIMPIEZA_HORA_UTC, "minute": 0, "misfire_grace_time": 3600}),
)


def registrar(scheduler, ejecutar: Callable[[TareaPeriodica], Callable]) -> None:
    """Agrega las tareas a un scheduler de APScheduler. `ejecutar(tarea)` devuelve lo que se llamara al dispararse."""
    for tarea in TAREAS:
        scheduler.add_job(ejecutar(tarea), tarea.disparador, id=tarea.nombre, replace_existing=True, **tarea.argumentos)


def iniciar_en_la_api(scheduler, modo: str) -> bool:
    """
    Modo "api": programa las tareas en el APScheduler de la API (se ejecutan directamente) y lo inicia.
    Modo "worker": no hace nada, porque las programa y ejecuta el worker aparte. Devuelve si las programo.
    """
    if modo != "api":
        return False
    registrar(scheduler, lambda tarea: tarea.callable())
    scheduler.start()
    return True
