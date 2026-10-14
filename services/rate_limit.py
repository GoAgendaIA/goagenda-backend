"""
Limitador de peticiones en memoria (ventana deslizante) para el chat publico.

Es por proceso, igual que el mapa de WebSockets (services/realtime.py): sirve
para una sola instancia de la API, que es como se despliega hoy. Con varias
instancias habria que moverlo a Redis. Protege el costo de OpenAI de rafagas
y abusos; no pretende ser un WAF.
"""

import threading
import time
from collections import deque

_lock = threading.Lock()
_eventos: dict[str, deque] = {}
_MAX_CLAVES = 20000  # tope de memoria: ante un barrido de claves, se purga lo vencido


def permitir(clave: str, limite: int, ventana_segundos: int) -> bool:
    """True si la clave aun tiene cupo en la ventana (y lo consume); False si ya lo agoto."""
    if limite <= 0:
        return True
    ahora = time.monotonic()
    with _lock:
        cola = _eventos.setdefault(clave, deque())
        corte = ahora - ventana_segundos
        while cola and cola[0] <= corte:
            cola.popleft()
        if len(cola) >= limite:
            return False
        cola.append(ahora)
        if len(_eventos) > _MAX_CLAVES:
            _purgar(ahora - 3600)
        return True


def _purgar(corte: float) -> None:
    for clave in [k for k, c in _eventos.items() if not c or c[-1] <= corte]:
        del _eventos[clave]


def reiniciar() -> None:
    with _lock:
        _eventos.clear()
