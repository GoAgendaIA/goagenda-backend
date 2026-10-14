# Evaluacion del asistente (T2.1)

Conjunto de **34 conversaciones de referencia** (agendar, gestionar citas,
identificacion, humano, seguridad, domicilios, abonos y equipo) para comprobar,
**antes y despues** de cada cambio de prompt, herramientas o modelo, que el
asistente sigue haciendo lo correcto y cuantos tokens consume.

## Cuando usarla

Siempre que se toque algo que cambia lo que ve el modelo:

- `CHAT_PROMPT_VERSION` (v1 / v2) o cualquier edicion de `agent/prompts*.py`
- `CHAT_TOOLS_VERSION` (v1 / v2) o las descripciones de `agent/tools.py`
- `CHAT_MODEL_NAME`, `CHAT_HISTORIAL_MAX_TURNOS`, temperatura

## Como correrla

Necesita **una clave de OpenAI propia de la evaluacion** (no la de produccion).
Cuesta del orden de US$ 0.5 a 1.0 con `gpt-4.1-mini` y tiene un tope de gasto.

```bash
# 1) linea base con la configuracion actual
EVAL_LIVE=1 EVAL_OPENAI_API_KEY=sk-... EVAL_ETIQUETA=base \
  venv/bin/python -m pytest tests/test_evaluacion_en_vivo.py -s

# 2) con el prompt compacto
CHAT_PROMPT_VERSION=v2 EVAL_LIVE=1 EVAL_OPENAI_API_KEY=sk-... EVAL_ETIQUETA=prompt-v2 \
  venv/bin/python -m pytest tests/test_evaluacion_en_vivo.py -s

# 3) comparar
venv/bin/python -m evaluacion.comparar evaluacion/reportes/<base>.json evaluacion/reportes/<prompt-v2>.json
```

Variables: `EVAL_TOPE_USD` (default 1.5), `EVAL_MIN_ACIERTO` (default 0.85),
`EVAL_MODELO` (default `gpt-4.1-mini`), `EVAL_ETIQUETA`.

## Que garantiza el aislamiento

- Supabase es un doble **en memoria**; no hay base real.
- WhatsApp, push, Wompi y geocodificacion estan reemplazados.
- Con `EVAL_LIVE=1` solo se puede resolver `api.openai.com`: cualquier otro host falla.
- Los reportes (`evaluacion/reportes/`) estan en `.gitignore`.

## Que mide cada escenario

Cada uno declara herramientas que debe / no debe llamar, textos que debe / no
debe contener, citas creadas y estados finales. Ademas, **todos** verifican dos
invariantes del producto: ninguna hora en 24 h al cliente y ningun precio que no
salga del catalogo, los recargos o el abono.

El reporte guarda, por escenario: llamadas al modelo, tokens de entrada /
cacheados / salida, costo, duracion y los fallos.

## Como leer una comparacion

- `regresiones`: escenarios que **antes pasaban y ahora fallan** (bloquean el cambio).
- `mejoras`: al reves.
- `tokens_entrada.variacion_pct` y `costo_usd.variacion_pct`: el ahorro real.
- Un modelo no es determinista: si un escenario falla solo a veces, repetir la
  corrida antes de concluir.

## Limites

- Los mensajes del cliente traen los datos completos a proposito; no cubre
  conversaciones muy ambiguas ni muy largas (mas de ~12 turnos).
- Evalua comportamiento, no tono ni redaccion.
- Es una muestra de 34 casos: sirve para detectar regresiones, no para certificar calidad.
