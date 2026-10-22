# Plan de migración a Seenode (producción) manteniendo el VPS como pruebas

> Estado: **propuesta, nada implementado todavía.**
> Fecha del análisis: 2026-10-13.
> Las cifras de precio y las capacidades de Seenode/Redis Cloud salen de sus
> páginas públicas a esa fecha; revalidarlas antes de contratar.

## 1. Objetivo

- **Producción** pasa a Seenode: API, worker de tareas en segundo plano y SPA.
- El **VPS actual** (el que hoy despliega `.github/workflows/deploy.yml`) se
  conserva como ambiente de **pruebas/staging**.
- Ambos ambientes deben **comportarse igual**: mismo código, misma imagen/versión,
  mismos procesos. Solo cambian variables de entorno y credenciales.
- Base de datos: **Supabase plan gratis** (se mantiene; ver §10, riesgos).

## 2. Estado actual

| Pieza | Hoy |
|---|---|
| API | FastAPI en un contenedor Docker (`Dockerfile`, `uvicorn main:app`, puerto 8000) en el VPS |
| Tareas en segundo plano | `APScheduler` `BackgroundScheduler` **dentro del proceso de la API** (`main.py:287-292`): `revisar_y_enviar_recordatorios` cada 5 min y `expirar_solicitudes_vencidas` cada 15 min |
| Datos / auth | Supabase (`supabase-py` + JWT de Supabase Auth) y conexión directa `DATABASE_URL` para el checkpointer de LangGraph |
| Tiempo real | `GestorConexionesTiempoReal`: mapa de WebSockets **en memoria** del proceso de la API |
| CI/CD | GitHub Actions `deploy.yml`: tests (cobertura ≥ 90 %) → build `linux/arm64` → push a GHCR → SSH vía túnel Cloudflare → `docker compose pull && up -d` en el VPS |
| Front | SPA Angular (repo `go-agenda`) |

## 3. Arquitectura objetivo

```
                        ┌──────────────── Producción (Seenode) ────────────────┐
 Navegador ──HTTPS──►   │ SPA estática ($0)                                    │
                        │ API  (Web Standard 1 GB/0.5 CPU, $7)  ◄── WebSocket  │
                        │ Worker (Worker Basic 0.5 GB/0.2 CPU, $4)             │
                        └───────┬──────────────────┬───────────────────────────┘
                                │                  │
                         Supabase (gratis)   Redis Cloud (gratis, 30 MB)
                                                   ▲ broker Dramatiq

                        ┌──────────────── Pruebas (VPS) ───────────────────────┐
                        │ SPA · API · worker · redis (contenedor local)        │
                        └───────┬──────────────────────────────────────────────┘
                         Supabase (proyecto SEPARADO de prod)
```

Reglas de paridad:

1. **Mismos procesos en ambos**: `api`, `worker`, broker Redis. En el VPS el
   Redis es un contenedor más del compose (costo $0); en Seenode es Redis Cloud.
2. **Una sola base de código y un solo set de variables** (`.env.example`); lo
   único que difiere es el valor de cada variable.
3. **Proyecto de Supabase distinto por ambiente** (ver §7). Pruebas nunca
   toca datos de producción.

## 4. Cambios de código (una sola vez, sirven a ambos ambientes)

### 4.1 Sacar el scheduler de la API
- Quitar de `main.py` el bloque `BackgroundScheduler` (líneas ~287-292).
  Con API y worker separados, dejarlo duplicaría recordatorios de WhatsApp.
- Mantener los endpoints de prueba (`/test-reminders`, etc.) tal cual: llaman
  a la función directamente.

### 4.2 Broker y tareas (Dramatiq)
- Dependencias nuevas en `requirements.txt`: `dramatiq[redis]` (y `periodiq`
  solo si se elige esa opción de §4.3).
- `core/settings.py`: nueva variable `REDIS_URL` (obligatoria en el worker,
  opcional en la API mientras no encole nada).
- `worker.py` (nuevo, raíz): configura `RedisBroker(url=REDIS_URL)` y declara
  los actores:
  - `tarea_recordatorios` → `revisar_y_enviar_recordatorios()`
  - `tarea_expirar_pagos` → `expirar_solicitudes_vencidas()`
- Reintentos: `max_retries` bajo (2-3) y `min_backoff` ≥ 30 s. Las tareas
  deben ser **idempotentes** (hoy lo son en la práctica porque marcan el
  recordatorio como enviado; revisar al implementar).

### 4.3 Disparador periódico
Dramatiq no programa por sí solo. Opciones:

| Opción | Pro | Contra |
|---|---|---|
| **`scheduler.py` con APScheduler que solo encola** (recomendada) | Mínimo cambio, ya conocido | Hay que garantizar **una sola instancia** |
| `periodiq` | Estándar del ecosistema | Otra dependencia y otro proceso |

Recomendación: `scheduler.py` (APScheduler `BlockingScheduler`) que llama
`tarea_recordatorios.send()` / `tarea_expirar_pagos.send()`. Se lanza desde un
`start_worker.sh` en el mismo contenedor del worker:

```bash
python scheduler.py &
exec dramatiq worker --processes 1 --threads 4
```

`--processes 1` y **una sola réplica** del worker: es lo que impide
recordatorios duplicados.

### 4.4 Cuidado con el import de `agent/graph.py`
Importar `agent/graph.py` abre un pool de Postgres al importarse. El worker
solo importa `services/reminder_service.py` y
`services/wompi_payment_requests.py`; confirmar al implementar que **no**
arrastran `agent/graph` para no consumir conexiones del pooler de Supabase
(límite de 15 entre todos los procesos, ver `.env.example`).

### 4.5 Tiempo real
Hoy ni los recordatorios ni la expiración de pagos llaman a
`emitir_evento_cita`. Si en el futuro el worker necesita emitir eventos al
panel, **no funcionará**: el mapa de WebSockets vive en el proceso de la API.
Habría que pasar por Redis pub/sub (ya disponible con el broker). Anotado como
restricción, no se resuelve en esta migración.

## 5. Ambientes y variables

Todas las variables ya documentadas en `.env.example` + las nuevas:

| Variable | Pruebas (VPS) | Producción (Seenode) |
|---|---|---|
| `SUPABASE_URL` / `SUPABASE_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | Proyecto Supabase **de pruebas** | Proyecto Supabase **de producción** |
| `DATABASE_URL` | Session pooler (5432) del proyecto de pruebas | Session pooler (5432) del proyecto de prod |
| `REDIS_URL` | `redis://redis:6379/0` (contenedor del compose) | `rediss://…` de Redis Cloud |
| `OPENAI_API_KEY` | Clave con límite de gasto bajo | Clave de producción |
| `WHATSAPP_*`, `GOAGENDA_WHATSAPP_*` | Número/plantilla de prueba o `test` | Números reales |
| Wompi (credenciales por negocio, en BD) | Llaves **sandbox** de Wompi | Llaves de producción |
| `FIREBASE_CREDENTIALS_JSON` | Proyecto Firebase de pruebas (o el mismo, topic distinto) | Producción |
| `BAILEYS_SERVICE_URL` / `BAILEYS_INTERNAL_API_KEY` | URL del Baileys de pruebas | URL del Baileys de producción |
| `CORS`/origen del front | URL del front de pruebas | URL del front de producción |

> Pendiente: confirmar cómo está resuelto hoy el CORS (`main.py`) y si el
> origen sale de variable de entorno; si no, parametrizarlo antes del corte.

## 6. CI/CD

Principio: **lo que se prueba en el VPS es exactamente lo que va a producción.**

### 6.1 Pruebas (VPS) — pipeline actual, casi sin cambios
- `deploy.yml` sigue disparándose en `push` a `main`: tests → build → push →
  SSH al VPS. Es el ambiente de pruebas.
- Cambio mínimo: el `docker compose` del VPS gana dos servicios, `worker` y
  `redis`. Hoy ese compose vive **solo en el VPS** (`/opt/goagenda/backend`);
  recomendado versionarlo en el repo (`deploy/vps/docker-compose.yml`) para que
  los dos ambientes tengan su definición a la vista.
- El mismo imagen sirve para ambos procesos, cambiando solo el comando:
  - `api`: `uvicorn main:app --host 0.0.0.0 --port 8000`
  - `worker`: `sh start_worker.sh`

### 6.2 Producción (Seenode)
Opción recomendada: **servicio Git-backed de Seenode** (Python), no imagen:

- La imagen actual del pipeline es `linux/arm64`; Seenode con imagen exige
  `linux/amd64` y ese modo está en **Beta**, sin autodeploy al empujar imagen.
- Con Git, Seenode construye por su cuenta y soporta *auto-deploy on push*.

Flujo propuesto:

1. `main` → pipeline actual → VPS (pruebas). Se valida a mano / con smoke tests.
2. Cuando pruebas está OK, **se promueve**: un workflow manual
   (`workflow_dispatch`) o un tag `v*` hace *fast-forward* de `main` a la rama
   `production`.
3. Seenode (API y worker apuntando a la rama `production`) despliega solo al
   recibir el push.
4. Protección: entorno de GitHub `production` con aprobación requerida, y el
   workflow de promoción vuelve a correr los tests antes de mover la rama.

Servicios en Seenode:

| Servicio | Tipo | Plan | Build/start |
|---|---|---|---|
| `goagenda-api` | Web | Standard ($7) | `pip install -r requirements.txt` / `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| `goagenda-worker` | Worker | Basic ($4) | mismo build / `sh start_worker.sh` |
| `goagenda-front` | Static | $0 | build de Angular, deploy desde el repo `go-agenda` |

Variables de entorno marcadas como secretas en Seenode (`KEY*=valor`).

> Verificar en la documentación de Seenode, antes de implementar: el
> puerto/variable que inyecta la plataforma, si permite fijar la versión de
> Python (el repo usa 3.12, ver `.python-version`) y la página «Deploy with
> GitHub Actions».

## 7. Datos: Supabase por ambiente

- **Dos proyectos de Supabase** (pruebas y producción). Hoy el `.env` local
  apunta a producción (ver `CLAUDE.md`); el VPS no debe seguir compartiendo
  base con producción una vez migrado.
- Las migraciones son archivos `.sql` en la raíz (sin framework). Para tener
  ambos ambientes iguales hay que **ejecutar los mismos `.sql` en el mismo
  orden en los dos proyectos**. Dejar el orden escrito en
  `docs/ORDEN_MIGRACIONES.md` (pendiente de crear).
- Plan de datos para el corte:
  1. Crear el proyecto de pruebas y aplicar todos los `.sql`.
  2. El proyecto actual **pasa a ser producción** (no se migran datos), y el
     de pruebas es el nuevo. Así no hay migración de datos de clientes.
     **Decidido (2026-10-21).** Ver `docs/ORDEN_MIGRACIONES.md` para clonar el esquema y hacer respaldos.
  3. Configurar en pruebas negocios y usuarios de ejemplo.

## 8. Fases y orden de trabajo

| Fase | Contenido | Resultado verificable |
|---|---|---|
| 0 | Crear proyecto Supabase de pruebas, aplicar `.sql`, cuenta Redis Cloud | Entornos vacíos listos |
| 1 | Código: quitar scheduler de `main.py`, `worker.py`, `scheduler.py`, `start_worker.sh`, `REDIS_URL`, tests | `pytest` verde con cobertura ≥ 90 %; recordatorios salen vía worker en local |
| 2 | VPS: agregar `worker` y `redis` al compose, apuntar al Supabase de pruebas | Pruebas funcionando igual que hoy + worker |
| 3 | Seenode: crear API, worker, SPA con prueba gratis de 7 días; apuntar a Supabase de producción y Redis Cloud | Smoke test completo contra URLs de Seenode |
| 4 | Pipeline de promoción `main` → `production` con aprobación | Un despliegue de punta a punta |
| 5 | Corte: cambiar DNS/URL del front y de Meta/Wompi/Baileys a Seenode; apagar API de producción antigua si existía | Tráfico real en Seenode |
| 6 | Observación 1-2 semanas, medir RAM del worker y de la API; ajustar plan | Decisión de subir/bajar plan |

## 9. Checklist de corte (Fase 5)

- [ ] Webhook de Meta (`/webhook`) apunta a la URL de Seenode y el token de verificación coincide.
- [ ] Webhook de Wompi por negocio actualizado a la nueva URL.
- [ ] Baileys llama a la nueva URL con `x-api-key` correcta.
- [ ] Front de producción con la URL nueva de la API y WebSocket `wss://`.
- [ ] Prueba real: crear cita por el chat → llega confirmación por WhatsApp → llega recordatorio → expira pago vencido.
- [ ] Un solo worker y un solo scheduler corriendo en producción (revisar logs).
- [ ] `POST /test-reminders` desde el VPS **no** se ejecuta contra la BD de producción.

## 10. Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Supabase gratis **pausa** el proyecto tras 7 días sin actividad, sin backups, 500 MB | Caída de producción con clientes reales | Mientras no haya tráfico diario, un ping periódico; subir a Pro ($25) al tener clientes de pago |
| 0.5 GB / 0.2 CPU del worker se queda corto | Tareas lentas o OOM | Empezar en Basic, medir; subir a Standard (+$3) |
| Redis Cloud gratis (30 MB, sin SLA) se cae o se elimina por inactividad | Recordatorios no salen | Alerta si el worker no loguea en N minutos; plan B: Redis propio en Seenode (+$4, validar TCP) o `dramatiq-pg` |
| Dos schedulers activos (VPS y Seenode) contra la **misma** BD | Recordatorios de WhatsApp duplicados | Un proyecto de Supabase por ambiente (§7); nunca apuntar el VPS a la BD de producción |
| API en más de una réplica | WebSockets rotos (mapa en memoria) | Dejar la API en **1 réplica**; pub/sub en Redis antes de escalar |
| Región de Seenode lejos de Colombia/Supabase | Latencia por llamada | Verificar región al crear los servicios; elegir la más cercana a la región del proyecto de Supabase |
| Función de imagen Docker de Seenode en Beta | — | Se evita usando despliegue por Git |

## 11. Costo mensual

| Concepto | Costo |
|---|---|
| API Web Standard | $7 |
| Worker Basic | $4 |
| SPA estática | $0 |
| Supabase (prod + pruebas, plan gratis) | $0 |
| Redis Cloud gratis (prod) / contenedor en VPS (pruebas) | $0 |
| VPS de pruebas | ya existente |
| **Total** | **$11 / mes** |

Variantes: Redis propio en Seenode +$4; Supabase Pro en producción +$25.

## 12. Decisiones abiertas

1. Broker definitivo: Redis Cloud gratis (recomendado) vs. Redis propio en Seenode vs. `dramatiq-pg`.
2. Disparador periódico: `scheduler.py` con APScheduler (recomendado) vs. `periodiq`.
3. Despliegue de producción: Git-backed con rama `production` (recomendado) vs. imagen Docker.
4. Si pruebas usa o no un proyecto de Supabase propio (recomendado: sí).
5. ¿Qué otras tareas en segundo plano, además de las dos actuales, se moverán a Dramatiq (p. ej. envíos de WhatsApp)?
