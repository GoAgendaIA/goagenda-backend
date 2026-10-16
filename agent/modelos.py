"""
Parametros del cliente de OpenAI segun el tipo de modelo.

Los modelos de razonamiento (gpt-5.x, gpt-6.x -p. ej. gpt-6-luna-, o1/o3/o4) se comportan distinto:
- NO aceptan `temperature` (la API la rechaza con 400): hay que omitirla.
- Su limite de salida (`max_completion_tokens`) incluye el razonamiento oculto, no solo el texto
  que ve el cliente. Con el tope de 600 pensado para un modelo normal, el razonamiento podia
  consumirlo entero y la respuesta salia vacia o cortada: se usa un tope mayor.
- Aceptan `reasoning_effort` para limitar cuanto razonan (menos latencia y costo).
"""

PREFIJOS_RAZONAMIENTO = ("gpt-5", "gpt-6", "o1", "o3", "o4")


def es_modelo_de_razonamiento(nombre: str | None) -> bool:
    return (nombre or "").strip().lower().startswith(PREFIJOS_RAZONAMIENTO)


def parametros_llm(
    nombre: str | None,
    *,
    temperatura: float,
    max_tokens: int,
    max_tokens_razonamiento: int,
    esfuerzo_de_razonamiento: str | None = None,
) -> dict:
    """Argumentos de ChatOpenAI (aparte de modelo y clave) que corresponden a este modelo."""
    if not es_modelo_de_razonamiento(nombre):
        return {"temperature": temperatura, "max_tokens": max_tokens}

    parametros = {"max_tokens": max(max_tokens, max_tokens_razonamiento)}
    if esfuerzo_de_razonamiento:
        parametros["reasoning_effort"] = esfuerzo_de_razonamiento
    return parametros
