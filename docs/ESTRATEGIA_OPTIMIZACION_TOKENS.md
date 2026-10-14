# Estrategia de optimización de tokens (OpenAI)

> Estado: **en implementación en la rama `feat/optimizacion-tokens`** (ver §8). Sin desplegar.
> Fecha: 2026-10-13. Prioridad **P0**: va antes de la migración a Seenode.
> Las cifras de tokens de §2 son **mediciones aproximadas** (caracteres / 4) sobre el
> código real; la línea base exacta se fija en la Fase 0 con `usage` de la API.

## 1. Problema

El consumo de OpenAI amenaza con agotar los créditos. El costo no crece solo con el
número de clientes: **cada turno de chat dispara varias llamadas al modelo y cada
llamada vuelve a enviar el prompt, las herramientas y todo el historial**.

## 2. Diagnóstico (medido en el código)

| # | Hallazgo | Dato | Impacto |
|---|---|---|---|
| 1 | Prompt del sistema enorme y reconstruido en cada llamada | ~5.9 k tokens (`agent/prompts.py`, 23 KB) | Se paga en **cada** llamada del turno |
| 2 | Esquemas de las 14 herramientas | ~3.0 k tokens (docstrings largas) | Se paga en **cada** llamada |
| 3 | Fijo por llamada ≈ **8.8 k tokens** antes de contar el historial | 5.9 k + 3.0 k | Un turno con 4-6 llamadas = 35-55 k tokens de entrada |
| 4 | Historial completo sin recorte | `state["messages"]` íntegro (`agent/graph.py:222`) | Crece con la conversación; las salidas de herramientas viejas viajan para siempre |
| 5 | Salidas de herramientas como `str(dict)` | `ToolMessage(content=str(resultado))` (`agent/tools.py`) | Texto verboso (repr de Python) reenviado cada vez |
| 6 | **Caché de prompt desaprovechada** | El prefijo estable del prompt es de ~29 tokens: la fecha con minutos y el teléfono van al principio y cambian en cada llamada | El descuento de tokens cacheados no se aplica a ~5.9 k tokens |
| 7 | Llamada extra por la red anti-alucinación | Si la respuesta menciona precios no respaldados, se descarta y se llama otra vez (`graph.py`) | Hasta 2x en ese turno; la primera respuesta se paga y se tira |
| 8 | Reintentos multiplicados | `max_retries=3` del cliente × `LLM_MAX_INTENTOS=3` propio | Hasta 9 intentos ante un fallo |
| 9 | Sin tope de salida | No hay `max_tokens` | Respuestas largas sin límite |
| 10 | **Presupuesto solo alerta, no corta** | `_verificar_presupuesto` envía un push al 80 %, sin bloqueo | Un negocio (o un abuso) puede gastar sin techo |
| 11 | Mensaje del cliente sin límite de tamaño | `ChatMessageInput.mensaje: str` | Un solo mensaje enorme dispara el gasto |
| 12 | Chat público sin límite de peticiones | Endpoints públicos por diseño | Un tercero puede agotar los créditos |

Modelo de costo: `costo/conversación ≈ N_llamadas × (8.8 k fijos + historial + salida)`.
Reducir **cada** factor (llamadas, fijos, historial) y **abaratar los fijos** (caché) se
combinan multiplicando.

## 3. Principios (lo que hacen las grandes compañías)

1. **Medir antes de optimizar**: costo por conversación y por cita agendada, tasa de acierto de caché.
2. **Estático primero, dinámico al final**: el prefijo idéntico entre llamadas es lo que el proveedor cachea.
3. **Contexto mínimo suficiente**: ventana + resumen del historial; resultados de herramientas compactos.
4. **Menos llamadas**: lo determinista (botones, FAQ) no pasa por el modelo.
5. **Modelo adecuado a la tarea** (enrutamiento/cascada): el modelo barato para lo simple.
6. **Defensa en profundidad del gasto**: límites por IP, sesión, mensaje, negocio y cuenta.
7. **Evaluar antes de recortar**: un conjunto de conversaciones de referencia protege la calidad.
8. **Gobernanza**: presupuesto, alertas y revisión continua; que las regresiones rompan el CI.

## 4. Fases

### Fase 0: medición y salvaguardas (≈ 2 días)  ← empezar aquí
Detiene la hemorragia y fija la línea base.
- **Instrumentación:** registrar por llamada `input`, `cached`, `output`, tipo de llamada (turno normal, reintento, re-llamada anti-alucinación) y asociarla a la conversación (`chat_sessions`). Métricas: tokens por conversación, costo por cita agendada, % de caché.
- **Línea base:** 20-30 conversaciones reales/simuladas medidas con la API.
- **Salvaguardas de costo:**
  - límite de peticiones por IP y por sesión en el chat público;
  - `max_length` del mensaje (p. ej. 500 caracteres) y tope de turnos por sesión;
  - **corte duro** del presupuesto mensual por negocio (hoy solo alerta);
  - claves de OpenAI **separadas por ambiente** (producción, pruebas) con límite mensual en el panel de OpenAI;
  - `max_retries` del cliente a 0-1 (los reintentos ya los hace el código) y `max_tokens` de salida.

### Fase 1: ahorros rápidos sin cambiar el comportamiento (≈ 3 días)
- **Reordenar el prompt para la caché:** reglas estáticas al inicio; fecha (sin minutos), teléfono, empleado, servicio y catálogo al final. Verificar `cached_tokens` en la respuesta.
- **Historial:** ventana de los últimos N turnos + recorte de salidas de herramientas antiguas (dejar una línea de resumen) + resumen cada K turnos con el modelo barato.
- **Salidas de herramientas compactas:** JSON mínimo en lugar de `str(dict)`; topes (máx. N horarios, solo campos usados).
- **Secciones condicionales del prompt:** incluir domicilios solo si el negocio los tiene activos, abonos solo si algún servicio los requiere, empleados solo si hay más de uno.

### Fase 2: dieta de prompt y herramientas, con evaluación (≈ 1 semana)
- **Conjunto de evaluación** (30+ conversaciones doradas: agendar, cancelar, reprogramar, domicilio, abono, transferencia, intentos de inyección). Criterio: no bajar el acierto.
- **Prompt:** de ~5.9 k a ≤ 3 k tokens: quitar redundancia, mover reglas a código cuando se puedan imponer ahí (validaciones que hoy se piden por texto).
- **Herramientas:** de ~3.0 k a ≤ 1.5 k tokens: descripciones cortas; **gating por estado** (p. ej. sin teléfono registrado, no ofrecer reprogramar/cancelar).

### Fase 3: menos llamadas y modelo adecuado (≈ 1 semana)
- **Flujos deterministas:** si el cliente toca un botón de selección rápida, resolver el paso en código sin invocar el modelo.
- **Eliminar la re-llamada anti-alucinación** adelantando la consulta del catálogo cuando la intención es obvia (precios, servicios).
- **Fusionar herramientas** que casi siempre van juntas.
- **Enrutamiento de modelo:** clasificar el turno; el modelo más barato para saludos, FAQ y pasos simples, y el actual solo para los complejos. Evaluar con el conjunto de §Fase 2 los modelos económicos disponibles (la página de precios de OpenAI muestra familias nuevas más baratas que `gpt-4.1-mini`; validar calidad antes de cambiar).

### Fase 4: caché de aplicación y operación continua
- **Respuestas deterministas para FAQ:** horarios, dirección, servicios y precios se responden con plantilla desde la base, sin LLM.
- **Caché por negocio** con invalidación al cambiar servicios/horarios.
- **Gobierno:** dashboard de costo por cita, alertas (Grafana), revisión mensual y **prueba en CI** que falle si el prompt o los esquemas superan su presupuesto de tokens.

## 5. Metas (KPIs) a validar con la línea base

| KPI | Hoy (estimado) | Meta |
|---|---|---|
| Tokens fijos por llamada | ~8.8 k | ≤ 4.5 k |
| Tokens de entrada **facturados a tarifa completa** | ~100 % | ≤ 30 % (caché) |
| Llamadas al modelo por conversación | 6-10 (a medir) | −30 % |
| Costo por cita agendada | línea base | **−60 a −75 %** |
| Tasa de acierto de caché | ~0 % del prompt | ≥ 70 % |
| Calidad (evaluación dorada) | línea base | sin degradación |

> Las metas combinan palancas que se multiplican; son hipótesis hasta medir la línea base (Fase 0).

## 6. Riesgos

| Riesgo | Mitigación |
|---|---|
| Recortar prompt/herramientas degrada la calidad (más alucinaciones) | Conjunto de evaluación + despliegue gradual por negocio |
| Resumir el historial pierde datos clave (fecha elegida, servicio) | Mantener un bloque de "estado de la reserva" estructurado fuera del resumen |
| Límites demasiado estrictos molestan a clientes reales | Umbrales generosos, mensaje amable, métricas de bloqueos |
| Cambiar de modelo altera el comportamiento (parámetros como `temperature`) | Probar en pruebas con el mismo conjunto antes de producción |
| Caché de FAQ desactualizada | Invalidar al editar servicios/horarios |

## 7. Orden de implementación

1. **Fase 0 completa** (cualquier ahorro posterior se mide contra esta línea).
2. Fase 1 (reordenar prompt + historial + salidas compactas).
3. Conjunto de evaluación → Fase 2.
4. Fase 3 y Fase 4.
5. **Antes de migrar a Seenode:** Fases 0 y 1 como mínimo (el costo variable es el principal riesgo operativo de producción).

## 8. Estado de implementación

| Tarea | Estado | Pendiente |
|---|---|---|
| T0.1 Instrumentar tokens | Código listo y probado | Correr `tokens_uso_ia.sql` en Supabase y medir la línea base con tráfico real |
| T0.2 Salvaguardas de costo | Código listo y probado (mensaje largo, límites por IP/sesión, corte duro con aviso al dueño una vez por mes, `max_retries=0`, `max_tokens`) | Claves de OpenAI separadas por ambiente con límite mensual (operativo). **Decisión tomada:** activar `AI_PRESUPUESTO_CORTE_DURO=true` tras la primera semana de datos de T0.1 |
| T1.1 Reordenar prompt para la caché | Código listo y probado | Verificar `cached_tokens` en producción (≥ 70 %) |
| T1.2 Historial | Código listo y probado (ventana de 12 turnos y resultados de consulta superados reemplazados por un marcador; solo afecta lo que viaja al modelo) | Verificar en producción; resumen con modelo barato queda para una fase posterior |
| T1.3 Salidas de herramientas compactas | Código listo y probado: lista de servicios **-79 %** (~494 → ~105 tokens para 6 servicios) | Medir el efecto total en la línea base |
| T1.4 Secciones condicionales | Código listo y probado: prompt de un negocio simple **-12 %** (~5.7 k → ~5.0 k tokens) | La reducción grande del prompt viene en T2.2 (con evaluación) |
| T2.1 Evaluación | Código listo y probado: 34 escenarios, ejecutor con tope de gasto y reporte comparable (`evaluacion/`) | **Correrla en vivo con una clave propia** (`evaluacion/README.md`) para fijar la línea base |
| T2.2 Prompt compacto | Código listo y probado, detrás de `CHAT_PROMPT_VERSION=v2` (apagado). Simple: ~5.0 k → ~2.1 k tokens (**−57 %**) | Evaluación en vivo v1 vs v2 antes de activarlo |
| T2.3 Herramientas compactas | Código listo y probado, detrás de `CHAT_TOOLS_VERSION=v2` (apagado). ~2.85 k → ~1.5 k tokens (**−48 %**) | Evaluación en vivo antes de activarlo |
| T3.1 Menos llamadas | Código listo y probado, detrás de `CHAT_FLUJOS_DETERMINISTAS` (apagado): saludo sin modelo, resumen de confirmación sin llamada de relevo y consulta de servicios en la primera llamada | Evaluación en vivo y medir llamadas por conversación (meta −30 %) |
| T3.2 Enrutamiento de modelo | Código listo y probado, detrás de `CHAT_ENRUTAMIENTO=relay` + `CHAT_MODEL_NAME_SIMPLE` (apagado) | Evaluar modelos baratos con T2.1 antes de elegir uno |
| Fase 4 | Sin empezar | — |

### Notas de la implementación

- **Orden de despliegue:** `tokens_uso_ia.sql` es opcional (si las columnas no existen, el registro cae al formato anterior). Se puede correr antes o después.
- **Límites en memoria:** el limitador es por proceso (igual que el mapa de WebSockets); sirve con una sola instancia de la API.
- **`X-Forwarded-For`:** se usa la última entrada (la que agrega el proxy de confianza). Si la API queda sin proxy delante, un cliente podría falsear el encabezado y esquivar el límite por IP; el límite por sesión sigue activo.
- **Cambio de comportamiento a vigilar:** la fecha del prompt ya no lleva minutos (`HH:00`), y las respuestas del modelo quedan acotadas a `LLM_MAX_TOKENS_SALIDA` tokens.

### Decisiones confirmadas

- **Corte duro de presupuesto:** se activará (`AI_PRESUPUESTO_CORTE_DURO=true`) **después de la primera semana de datos** de T0.1, con el presupuesto por negocio ya revisado (`monthly_ai_budget_usd`).
- **Aviso al dueño:** al activarse el corte recibe un push («Asistente de IA pausado») una sola vez por mes. La marca persistente requiere `tokens_uso_ia.sql`; sin ella, el aviso se limita a uno por reinicio y mes.
- **Dónde se revisa y edita el presupuesto:** en el panel de super admin, **Negocios → «Presupuesto IA»** (muestra gasto del mes, presupuesto efectivo y barra de avance). Endpoints: `GET /admin/ai-budgets` y `PUT /admin/businesses/{id}/ai-budget` (0 < monto ≤ 1000 USD). Si un negocio no tiene valor guardado rige US$ 5.
- **Claves de OpenAI separadas por ambiente:** las crea el equipo en el panel de OpenAI (producción y pruebas, cada una con límite mensual).

### Notas de T1.2, T1.3 y T1.4

- **T1.3:** las salidas de `consultar_servicios/empleados/horas/zonas` pasan de `str(dict)` a JSON compacto, y la lista de servicios lleva solo `id`, `name`, `duration_minutes`, `price`, `offers_home_visit` (si hay domicilios) y los campos de abono cuando aplican. Los precios siguen como números: la red anti-alucinación los busca ahí.
- **T1.4:** las reglas de domicilios (2 bloques) solo se incluyen si el negocio tiene `home_visits_enabled`; las de pagos, solo si algún servicio del catálogo `requires_payment`. La regla de cancelación con pago (corta) se mantiene siempre.
- **T1.2:** se conservan los últimos `CHAT_HISTORIAL_MAX_TURNOS` (12) turnos del cliente. Se corta siempre en un mensaje humano para no dejar `ToolMessage` huérfanos. De cada consulta (horas, empleados, servicios, zonas) se conserva el resultado **más reciente** aunque sea de un turno anterior (el cliente elige la hora en el turno siguiente y el modelo aún necesita `equivalencias_am_pm_a_24h`).
- **Limitación conocida (T1.2):** pasados 12 turnos, datos dichos por el cliente al principio (p. ej. su nombre) dejan de viajar; el aviso insertado le indica al modelo que los pida de nuevo. Un bloque de «estado de la reserva» estructurado queda para después.

### Notas de T2.1, T2.2 y T2.3

- **T2.1:** `evaluacion/` contiene 34 conversaciones de referencia (agendar, gestionar, identificación, humano, seguridad, domicilios, abonos, equipo), un ejecutor que mide llamadas/tokens/costo con tope de gasto y un comparador de reportes. Verifica además dos invariantes en todos los escenarios: ninguna hora en 24 h al cliente y ningún precio inventado. Con `EVAL_LIVE=1` solo se puede resolver `api.openai.com`; Supabase, WhatsApp, Wompi y push son dobles.
- **T2.2:** `agent/prompts_v2.py` conserva cada regla distinta del producto (hay una prueba con ~40 referencias obligatorias: nombres de tools, campos y frases) y elimina la redundancia (reglas repetidas 2-3 veces). Mismo orden pensado para la caché. **Apagado por defecto**: sin evaluación en vivo no se puede afirmar que el modelo se comporte igual.
- **T2.3:** `agent/tools_schemas.py` deriva los esquemas de las tools reales (no se pueden desincronizar) y solo cambia descripciones y simplifica los opcionales.
- **Decisión de diseño — no se hace gating dinámico de herramientas por estado.** Las tools van al inicio del prefijo que OpenAI cachea: cambiar el conjunto entre llamadas invalida la caché (lo que T1.1 consiguió) y el ahorro de quitar unas pocas tools es menor que lo que cuesta perderla. Las propias tools ya devuelven un mensaje amable si falta el teléfono o la cita.
- **Resultado combinado (aprox. caracteres/4):** fijo por llamada de un negocio simple ~7.9 k → ~3.6 k tokens (**−54 %**).

### Cómo activar v2 de forma segura

1. Correr la evaluación con la configuración actual (`EVAL_ETIQUETA=base`).
2. Correrla con `CHAT_PROMPT_VERSION=v2` y/o `CHAT_TOOLS_VERSION=v2` y compararlas con `python -m evaluacion.comparar`.
3. Si no hay regresiones, activarlo primero en el VPS de pruebas y luego en producción, vigilando `ia.tokens` en el Centro de métricas.

### Notas de T3.1 y T3.2

**T3.1: qué atajos hay y cuándo se activan**

| Atajo (`CHAT_FLUJOS_DETERMINISTAS`) | Qué hace | Cuándo NO aplica | Llamadas que evita |
|---|---|---|---|
| `saludo` | Un saludo puro ("hola", "buenas tardes") al inicio se responde en código con la lista real de servicios y los botones; deja en el historial el mismo par llamada/resultado de `consultar_servicios_disponibles` | Varios empleados sin chat fijo, ya hubo conversación, el mensaje trae algo más, cliente transferido | 2 |
| `confirmacion` | El resumen exitoso de `pedir_confirmacion_cita` se entrega tal cual | Resultados de error (los explica el modelo), varias herramientas a la vez | 1 por reserva |
| `servicios` | Ante "¿qué servicios tienen / cuánto cuesta?" la primera llamada ya es la consulta (antes: texto descartado por la red anti-alucinación y luego otra llamada) | El mensaje pide agendar o gestionar, o hay varios empleados sin elegir | 1 |

- **Hallazgo durante la implementación:** `ToolNode` de LangGraph le pone el nombre de la herramienta también a sus mensajes de error, así que el resumen exitoso se distingue por su contenido (termina en «¿Confirmo tu cita?», el mismo criterio de coherencia de botones) y no por el nombre.
- **No se hizo** una plantilla para el mensaje posterior a `crear_cita`: involucra links de pago exactos, domicilios y el tono del negocio; es de donde menos se gana y más se arriesga.

**T3.2: enrutamiento**

- Con `CHAT_ENRUTAMIENTO=relay`, la llamada que solo tiene que **relatar** el resultado de una consulta (servicios, horas, empleados, zonas, teléfono registrado, selecciones y consulta de citas) usa `CHAT_MODEL_NAME_SIMPLE`.
- **Siempre con el modelo principal:** `crear_cita` (link de pago exacto), cancelar, reprogramar y las herramientas de escalamiento.
- El costo se registra con el modelo real de cada llamada (tipo `relevo` en `ai_usage_log`).
- **Compromiso conocido:** la caché de prompt es por modelo; el modelo simple mantiene su propia caché (se calienta con sus propias llamadas).
- La clasificación es por posición en el flujo (qué acaba de ocurrir), no por un clasificador con LLM: es determinista y no cuesta tokens.
