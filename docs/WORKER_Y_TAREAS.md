# Worker y tareas en segundo plano (P3)

GoAgenda ejecuta tres tareas periódicas:

| Tarea | Frecuencia | Qué hace |
|---|---|---|
| `recordatorios` | cada 5 min | Envía por WhatsApp los recordatorios de citas próximas |
| `expirar_pagos` | cada 15 min | Expira las solicitudes de abono de Wompi vencidas y libera el horario |
| `limpiar_conversaciones` | diaria, 08:00 UTC | Elimina el historial de conversaciones con 3 días de inactividad |

El calendario vive en **un solo lugar**: `services/tareas_periodicas.py`.

## Dos modos de ejecución

Se elige con `TAREAS_EN_SEGUNDO_PLANO`:

| Modo | Quién ejecuta las tareas | Cuándo usarlo |
|---|---|---|
| `api` (**por defecto**) | Un APScheduler **dentro** de la API | Hoy, mientras haya una sola instancia de la API. Desplegar este cambio **no mueve nada** |
| `worker` | Un proceso aparte: `scheduler.py` las **encola** en Dramatiq/Redis y `worker.py` las **ejecuta** | API y worker desplegados por separado (Seenode, VPS de pruebas con worker) |

> **Nunca los dos a la vez.** Con la API en modo `api` y un worker corriendo, cada recordatorio sale dos veces. Si pones `worker`
> en la API pero no despliegas el worker, las tareas **dejan de correr sin error**.

## El worker

- `worker.py`: actores de Dramatiq, uno por tarea. Reintentos: máximo 3, espera mínima de 30 s, límite de 10 min por ejecución.
- `scheduler.py`: APScheduler que solo **encola**. Vive en el mismo contenedor que el worker.
- `start_worker.sh`: lanza los dos y, **si cualquiera muere, apaga el otro y termina con error** para que la plataforma reinicie el
  contenedor (un worker sin scheduler, o al revés, dejaría de enviar recordatorios sin avisar).
- **Una sola réplica y `--processes 1`**: es lo que impide los recordatorios duplicados.
- Variables: `REDIS_URL` (obligatoria), más las de siempre (`SUPABASE_*`, `WHATSAPP_*`, `GOAGENDA_*`, `DATABASE_URL`…).
- Las tareas son idempotentes: un recordatorio enviado queda marcado y un pago expirado ya no está pendiente.

**Conexiones a Postgres:** `recordatorios` y `expirar_pagos` no cargan el agente (hay una prueba que lo verifica). Solo
`limpiar_conversaciones` lo hace —una vez al día— y abre un pool. El pooler de Supabase admite **15 conexiones en total entre todos
los procesos**: dale al worker `DB_POOL_MAX_SIZE=2` (la API usa 10).

## Imágenes

El pipeline publica una imagen **multi-arquitectura** (`linux/amd64` + `linux/arm64`) en `ghcr.io/goagendaia/goagenda-backend`.
Cada host descarga la suya: el VPS (arm64) y Seenode (exige amd64). La misma imagen sirve para la API y el worker; solo cambia el comando:

| Proceso | Comando |
|---|---|
| API | `uvicorn main:app --host 0.0.0.0 --port 8000` (el del `Dockerfile`) |
| Worker | `/app/start_worker.sh` |

## VPS: agregar el worker (pruebas)

El `docker-compose.yml` del VPS vive solo en el servidor. Pasos, en este orden (cada uno es seguro por separado):

1. **Desplegar este cambio.** No cambia nada: la API sigue en modo `api`.
2. En `/opt/goagenda/backend/docker-compose.yml`, agregar junto al servicio de la API (ajusta el nombre del servicio y la red a como los tengas):

```yaml
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "64mb", "--maxmemory-policy", "noeviction"]

  worker:
    image: ghcr.io/goagendaia/goagenda-backend:latest
    restart: unless-stopped
    command: ["/app/start_worker.sh"]
    env_file: .env
    environment:
      REDIS_URL: redis://redis:6379/0
      DB_POOL_MAX_SIZE: "2"
    depends_on:
      - redis
```

3. En la API (mismo `.env` o su bloque `environment`), poner `TAREAS_EN_SEGUNDO_PLANO=worker`.
4. `docker compose up -d --remove-orphans`: arranca Redis y el worker y recrea la API **sin** scheduler.
5. Verificar: `docker compose logs worker | grep "encolando tareas"` y, a los 5 minutos, `docker compose logs worker | grep -i recordatorio`.

Para volver atrás: quitar `TAREAS_EN_SEGUNDO_PLANO=worker` (o ponerla en `api`), detener el `worker` y recrear la API.

## Seenode

Servicio de tipo **Worker** con la misma imagen y comando `/app/start_worker.sh`, `REDIS_URL` de Redis Cloud, `DB_POOL_MAX_SIZE=2`, y la
**API** con `TAREAS_EN_SEGUNDO_PLANO=worker`. Ver `docs/PLAN_MIGRACION_SEENODE.md` §6.2 (desplegar por Git o por imagen: la imagen
multi-arquitectura permite ambas; confirmar en el panel de Seenode si el tipo Worker acepta imagen).

## Limitación conocida

Las tareas no emiten eventos en tiempo real al panel: el mapa de WebSockets vive en el proceso de la API. Hoy ninguna lo necesita;
si alguna lo necesitara, habría que pasar por Redis pub/sub.
