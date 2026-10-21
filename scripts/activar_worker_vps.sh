#!/usr/bin/env bash
#
# P3 · Pasa las tareas en segundo plano (recordatorios, expiracion de pagos, retencion de conversaciones) de
# la API a un WORKER aparte con Redis, en el VPS. Ver docs/WORKER_Y_TAREAS.md.
#
# Que cambia en /opt/goagenda/backend/docker-compose.yml (con copia de seguridad):
#   - api:    + environment: TAREAS_EN_SEGUNDO_PLANO=worker   (la API deja de programar tareas)
#   - redis:  contenedor nuevo (sin puertos publicados)
#   - worker: contenedor nuevo, misma imagen, comando /app/start_worker.sh
# El .env NO se toca.
#
# Despues de levantarlo VERIFICA que el worker ejecute de verdad una tarea (el primer ciclo de recordatorios,
# hasta 6 minutos) y, si algo falla, restaura el compose anterior solo. Para volver atras a mano:
#   cp docker-compose.yml.bak.<fecha> docker-compose.yml && docker compose up -d --remove-orphans
#
# Correr como el usuario dueño de /opt/goagenda (deploy).  SOLO_CAMBIAR=1 no espera la verificacion.

set -Eeuo pipefail

DIR="${DIR:-/opt/goagenda/backend}"
IMAGEN="${IMAGEN:-ghcr.io/goagendaia/goagenda-backend:latest}"
cd "$DIR"

COMPOSE=""
for f in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do [ -f "$f" ] && { COMPOSE="$f"; break; }; done
[ -n "$COMPOSE" ] || { echo "No hay docker-compose en $DIR"; exit 1; }
grep -q "^  worker:" "$COMPOSE" && { echo "$COMPOSE ya tiene un servicio 'worker': nada que hacer."; exit 0; }
[ -f .env ] || { echo "No existe $DIR/.env"; exit 1; }
grep -q "^WOMPI_ENCRYPTION_KEY=." .env || { echo "✗ Falta WOMPI_ENCRYPTION_KEY en .env: el worker (tarea de pagos) no arranca sin ella."; exit 1; }

COPIA="$COMPOSE.bak.$(date +%Y%m%d-%H%M%S)"
cp "$COMPOSE" "$COPIA"
echo "Copia de seguridad: $DIR/$COPIA"

volver_atras() {
  echo; echo "✗ $1"
  echo "Restaurando el compose anterior (la API vuelve a ejecutar las tareas)..."
  cp "$COPIA" "$COMPOSE"
  docker compose up -d --remove-orphans >/dev/null 2>&1 || true
  echo "Listo: todo volvio a como estaba."
  exit 1
}

# 1) Editar el compose (texto): la API en modo worker + servicios nuevos al final de `services:`.
python3 - "$COMPOSE" "$IMAGEN" <<'PY' || volver_atras "No se pudo editar el compose automaticamente (su estructura es distinta a la esperada): editalo a mano segun docs/WORKER_Y_TAREAS.md."
import re, sys
ruta, imagen = sys.argv[1], sys.argv[2]
lineas = open(ruta).read().split("\n")

# Servicio api: debe existir y no tener ya `environment:`.
try:
    i_api = next(i for i, l in enumerate(lineas) if re.match(r"^  api:\s*$", l))
except StopIteration:
    sys.exit("no hay un servicio 'api'")
fin_api = next((i for i in range(i_api + 1, len(lineas)) if re.match(r"^  \S", lineas[i]) or re.match(r"^\S", lineas[i])), len(lineas))
bloque = lineas[i_api:fin_api]
if any(re.match(r"^    environment:", l) for l in bloque):
    sys.exit("la api ya tiene 'environment:'")
# Insertar despues del ultimo elemento no vacio del bloque de la api.
ultimo = max(i for i in range(i_api, fin_api) if lineas[i].strip())
lineas[ultimo + 1:ultimo + 1] = ["    environment:", "      TAREAS_EN_SEGUNDO_PLANO: worker"]

# Nada de claves de primer nivel despues de `services:` (volumes:, networks:...): los servicios van al final.
if any(re.match(r"^\S", l) and not l.startswith("services:") and not l.startswith("#") for l in lineas[lineas.index("services:") + 1:] if l.strip()):
    sys.exit("hay claves de primer nivel despues de 'services:'")

while lineas and not lineas[-1].strip():
    lineas.pop()
lineas += f"""
  redis:
    image: redis:7-alpine
    container_name: goagenda-redis
    restart: unless-stopped
    # Cola de tareas: sin persistencia (las tareas son periodicas y se vuelven a encolar) y con tope de memoria.
    command: ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "64mb", "--maxmemory-policy", "noeviction"]

  worker:
    image: {imagen}
    container_name: goagenda-worker
    restart: unless-stopped
    command: ["/app/start_worker.sh"]
    env_file:
      - .env
    environment:
      REDIS_URL: redis://redis:6379/0
      # El pooler de Supabase admite 15 conexiones en total entre todos los procesos: la API usa DB_POOL_MAX_SIZE
      # del .env y el worker solo necesita unas pocas (solo la retencion diaria abre el pool).
      DB_POOL_MAX_SIZE: "2"
    depends_on:
      - redis
""".split("\n")
open(ruta, "w").write("\n".join(lineas).rstrip("\n") + "\n")
PY
docker compose config -q 2>/tmp/_compose_err || { cat /tmp/_compose_err | head -5; volver_atras "El compose resultante no es valido."; }
echo "Compose actualizado (api en modo worker + redis + worker)."

# 2) Levantar. Primero Redis y el worker, y SOLO DESPUES recrear la API sin scheduler: asi no hay un hueco sin tareas.
docker compose pull redis worker >/dev/null 2>&1 || true
docker compose up -d redis worker >/dev/null || volver_atras "No arrancaron redis/worker."
echo -n "Esperando al worker"
LISTO=""
for _ in $(seq 1 30); do
  # El log se captura ANTES de buscar: `docker compose logs | grep -q` falla al azar con pipefail (grep cierra la tuberia
  # al primer acierto y docker recibe SIGPIPE).
  LOG="$(docker compose logs worker 2>/dev/null || true)"
  if grep -q "encolando tareas" <<<"$LOG"; then LISTO=1; break; fi
  echo -n "."; sleep 2
done
echo
[ -n "$LISTO" ] || { docker compose logs --tail 15 worker 2>&1 | sed 's/^/    /'; volver_atras "El worker no arranco (no se ve 'encolando tareas')."; }
PING="$(docker compose exec -T redis redis-cli ping 2>/dev/null || true)"
[ "${PING//[$'\r\n ']/}" = "PONG" ] || volver_atras "Redis no responde."
echo "  ✓ worker y redis arriba"

docker compose up -d --remove-orphans >/dev/null || volver_atras "No se pudo recrear la API."
echo "  ✓ API recreada en modo worker (ya no programa tareas)"
[ "${SOLO_CAMBIAR:-0}" = "1" ] && { echo "SOLO_CAMBIAR=1: no se espera la verificacion."; exit 0; }

# 3) Verificar que el worker EJECUTA una tarea: el scheduler encola los recordatorios cada 5 minutos.
echo -n "Esperando el primer ciclo de recordatorios (hasta 6 min)"
for _ in $(seq 1 72); do
  LOG="$(docker compose logs worker 2>/dev/null || true)"
  if grep -q "\[recordatorios\] inicio ciclo" <<<"$LOG"; then
    echo; echo "  ✓ el worker ejecuto el ciclo de recordatorios"
    echo
    echo "✓ Listo: las tareas corren en el worker. La API ya no ejecuta ninguna."
    echo "  Para volver atras:  cd $DIR && cp $COPIA $COMPOSE && docker compose up -d --remove-orphans"
    exit 0
  fi
  echo -n "."; sleep 5
done
echo
docker compose logs --tail 20 worker 2>&1 | sed 's/^/    /'
volver_atras "En 6 minutos el worker no ejecuto ningun ciclo de recordatorios: no se deja a la API sin tareas."
