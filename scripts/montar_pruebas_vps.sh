#!/usr/bin/env bash
#
# P4 · Monta el ambiente de PRUEBAS en el VPS sin tocar produccion. Ver docs/AMBIENTE_DE_PRUEBAS.md.
#
# Que hace (cada paso es idempotente y guarda copia de lo que cambia):
#   1. /opt/goagenda/pruebas con su PROPIO docker-compose (proyecto "goagenda-pruebas": el deploy de produccion corre
#      `up --remove-orphans` en /opt/goagenda/backend y no lo ve) con:
#        api-pruebas -> 127.0.0.1:8001  (misma imagen del backend, SU .env, sin tareas en segundo plano)
#        app-pruebas -> 127.0.0.1:8081  (front compilado con el environment de pruebas, tag "pruebas")
#   2. nginx: sitio nuevo api-pruebas.goagenda.online -> 8001 y pruebas.goagenda.online -> 8081. El sitio PHP viejo de
#      pruebas se mueve a un respaldo (no se borra /var/www). Los bloques de app. y api. NO se tocan.
#   3. Cloudflare Tunnel: agrega api-pruebas.goagenda.online a /etc/cloudflared/config.yml y reinicia cloudflared
#      (corta el tunel unos segundos, tambien el de produccion; por eso pide confirmacion).
#
# SEGURIDAD: api-pruebas solo arranca si /opt/goagenda/pruebas/.env existe y su SUPABASE_URL es DISTINTA de la de
# produccion. Si no, se monta todo lo demas y la API de pruebas queda apagada: pruebas nunca toca los datos reales.
#
# Correr como un usuario con sudo y acceso a docker (goagenda):   bash scripts/montar_pruebas_vps.sh
# Volver atras:                                                    bash scripts/montar_pruebas_vps.sh --revertir
# SIN_CONFIRMAR=1 no pregunta antes de reiniciar cloudflared.

set -Eeuo pipefail

DIR_PRUEBAS="${DIR_PRUEBAS:-/opt/goagenda/pruebas}"
DIR_PROD="${DIR_PROD:-/opt/goagenda/backend}"
NGINX_SITIOS="${NGINX_SITIOS:-/etc/nginx/sites-enabled}"
NGINX_DISPONIBLES="${NGINX_DISPONIBLES:-/etc/nginx/sites-available}"
NGINX_RESPALDOS="${NGINX_RESPALDOS:-/etc/nginx/respaldos-pruebas}"
CF_CONFIG="${CF_CONFIG:-/etc/cloudflared/config.yml}"
HOST_FRONT="${HOST_FRONT:-pruebas.goagenda.online}"
HOST_API="${HOST_API:-api-pruebas.goagenda.online}"
IMAGEN_API="${IMAGEN_API:-ghcr.io/goagendaia/goagenda-backend:latest}"
IMAGEN_FRONT="${IMAGEN_FRONT:-ghcr.io/goagendaia/go-front:pruebas}"
SITIO_NGINX="goagenda-pruebas"
# SUDO= (vacio) para correr como root o en pruebas del propio script.
SUDO="${SUDO-sudo}"
ESTAMPA="$(date +%Y%m%d-%H%M%S)"

ok()   { echo "✓ $*"; }
aviso(){ echo "! $*"; }
falla(){ echo "✗ $*" >&2; exit 1; }

# ---------------------------------------------------------------- volver atras
if [ "${1:-}" = "--revertir" ]; then
  echo "Revirtiendo el ambiente de pruebas (produccion no se toca)..."
  if [ -f "$DIR_PRUEBAS/docker-compose.yml" ]; then
    (cd "$DIR_PRUEBAS" && docker compose down) && ok "contenedores de pruebas detenidos"
  fi
  $SUDO rm -f "$NGINX_SITIOS/$SITIO_NGINX"
  PHP_RESPALDO="$($SUDO ls -t "$NGINX_RESPALDOS"/pruebas-php.* 2>/dev/null | head -1 || true)"
  if [ -n "$PHP_RESPALDO" ]; then
    $SUDO cp "$PHP_RESPALDO" "$NGINX_SITIOS/pruebas" && ok "sitio PHP de pruebas restaurado"
  fi
  $SUDO nginx -t && $SUDO systemctl reload nginx && ok "nginx recargado"
  CF_RESPALDO="$($SUDO ls -t "$CF_CONFIG".bak.* 2>/dev/null | tail -1 || true)"
  if [ -n "$CF_RESPALDO" ] && grep -q "$HOST_API" "$CF_CONFIG"; then
    $SUDO cp "$CF_RESPALDO" "$CF_CONFIG" && $SUDO systemctl restart cloudflared && ok "tunel restaurado ($CF_RESPALDO)"
  fi
  echo "Listo. $DIR_PRUEBAS queda en disco (con su .env) por si lo quieres revisar."
  exit 0
fi

command -v docker >/dev/null || falla "docker no esta instalado"
command -v nginx  >/dev/null || falla "nginx no esta instalado"

# ---------------------------------------------------------------- 1. compose de pruebas
if [ ! -d "$DIR_PRUEBAS" ]; then
  $SUDO mkdir -p "$DIR_PRUEBAS"
  $SUDO chown "$(id -u):$(id -g)" "$DIR_PRUEBAS"
fi
cd "$DIR_PRUEBAS"

if [ -f docker-compose.yml ]; then
  ok "docker-compose.yml de pruebas ya existe (no se sobrescribe)"
else
  cat > docker-compose.yml <<YAML
name: goagenda-pruebas

services:
  api-pruebas:
    image: ${IMAGEN_API}
    container_name: goagenda-api-pruebas
    restart: unless-stopped
    env_file: .env
    environment:
      # Pruebas no manda recordatorios ni limpia conversaciones: no hay worker y la API no programa tareas.
      TAREAS_EN_SEGUNDO_PLANO: worker
      DB_POOL_MAX_SIZE: "3"
    ports:
      - "127.0.0.1:8001:8000"

  app-pruebas:
    image: ${IMAGEN_FRONT}
    container_name: goagenda-app-pruebas
    restart: unless-stopped
    ports:
      - "127.0.0.1:8081:80"
YAML
  ok "docker-compose.yml de pruebas creado"
fi

# Valor de SUPABASE_URL de un .env, sin comillas ni espacios.
supabase_url() { [ -f "$1" ] && grep -E '^SUPABASE_URL=' "$1" | head -1 | cut -d= -f2- | sed -e "s/[\"' ]//g" || true; }
SUPABASE_PROD="$(supabase_url "$DIR_PROD/.env")"
SUPABASE_PRUEBAS="$(supabase_url .env)"

API_LISTA=0
if [ ! -f .env ]; then
  aviso "No existe $DIR_PRUEBAS/.env: api-pruebas queda APAGADA hasta que crees el Supabase de pruebas y su .env."
elif [ -z "$SUPABASE_PRUEBAS" ]; then
  aviso "El .env de pruebas no tiene SUPABASE_URL: api-pruebas queda APAGADA."
elif [ "$SUPABASE_PRUEBAS" = "$SUPABASE_PROD" ]; then
  aviso "El .env de pruebas apunta al MISMO Supabase que produccion: api-pruebas queda APAGADA (pruebas no toca datos reales)."
else
  API_LISTA=1
  ok "el .env de pruebas usa un Supabase distinto al de produccion"
fi

SERVICIOS="app-pruebas"
[ "$API_LISTA" = 1 ] && SERVICIOS="api-pruebas app-pruebas"
docker compose pull $SERVICIOS || aviso "no se pudo bajar alguna imagen (el tag 'pruebas' del front aparece tras el primer deploy del front con esta version)"
docker compose up -d $SERVICIOS && ok "levantado: $SERVICIOS"

# ---------------------------------------------------------------- 2. nginx
$SUDO mkdir -p "$NGINX_RESPALDOS" "$NGINX_DISPONIBLES"
DESTINO_NGINX="$NGINX_DISPONIBLES/$SITIO_NGINX"
TMP_NGINX="$(mktemp)"
cat > "$TMP_NGINX" <<NGINX
server {
    listen 80;
    server_name ${HOST_FRONT};

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

server {
    listen 80;
    server_name ${HOST_API};

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
$SUDO cp "$TMP_NGINX" "$DESTINO_NGINX"
rm -f "$TMP_NGINX"

PHP_VIEJO=0
if [ -e "$NGINX_SITIOS/pruebas" ]; then
  # Se COPIA al respaldo y se quita de sites-enabled: dos sitios con el mismo server_name chocarian.
  $SUDO cp -L "$NGINX_SITIOS/pruebas" "$NGINX_RESPALDOS/pruebas-php.$ESTAMPA"
  $SUDO rm -f "$NGINX_SITIOS/pruebas"
  PHP_VIEJO=1
  ok "sitio PHP de pruebas desactivado (respaldo: $NGINX_RESPALDOS/pruebas-php.$ESTAMPA)"
fi
$SUDO ln -sfn "$DESTINO_NGINX" "$NGINX_SITIOS/$SITIO_NGINX"

if $SUDO nginx -t 2>/dev/null; then
  $SUDO systemctl reload nginx && ok "nginx recargado"
else
  # Deshace lo de nginx y deja todo como estaba antes de este paso.
  $SUDO rm -f "$NGINX_SITIOS/$SITIO_NGINX"
  [ "$PHP_VIEJO" = 1 ] && $SUDO cp "$NGINX_RESPALDOS/pruebas-php.$ESTAMPA" "$NGINX_SITIOS/pruebas"
  $SUDO nginx -t || true
  falla "nginx rechazo la configuracion; se restauro lo anterior y no se toco el tunel."
fi

# ---------------------------------------------------------------- 3. tunel de Cloudflare
if grep -q "hostname: $HOST_API" "$CF_CONFIG"; then
  ok "el tunel ya tiene $HOST_API"
else
  CF_NUEVO="$(mktemp)"
  # Inserta la regla justo antes de la regla final (la que no tiene hostname, `http_status:404`).
  python3 - "$CF_CONFIG" "$HOST_API" "$CF_NUEVO" <<'PY'
import re, sys
ruta, host, salida = sys.argv[1:4]
lineas = open(ruta).read().splitlines()
for i in range(len(lineas) - 1, -1, -1):
    if re.match(r"\s*-\s*service:\s*http_status", lineas[i]):
        lineas[i:i] = [f"  - hostname: {host}", "    service: http://localhost:80", ""]
        break
else:
    sys.exit("no se encontro la regla final http_status en el config del tunel")
open(salida, "w").write("\n".join(lineas) + "\n")
PY
  command -v cloudflared >/dev/null && { cloudflared tunnel --config "$CF_NUEVO" ingress validate >/dev/null || falla "cloudflared rechazo el config nuevo; no se toco el tunel."; }
  if [ "${SIN_CONFIRMAR:-0}" != "1" ]; then
    echo
    read -r -p "Reiniciar cloudflared corta el tunel unos segundos (tambien el de produccion). ¿Continuar? [s/N] " r
    [ "$r" = "s" ] || [ "$r" = "S" ] || { rm -f "$CF_NUEVO"; falla "cancelado antes de tocar el tunel (nginx y los contenedores ya quedaron listos)."; }
  fi
  $SUDO cp "$CF_CONFIG" "$CF_CONFIG.bak.$ESTAMPA"
  $SUDO cp "$CF_NUEVO" "$CF_CONFIG"
  rm -f "$CF_NUEVO"
  # Tu sesion SSH entra POR este tunel: reiniciarlo la corta y mataria al script antes de comprobar nada. Por eso el
  # reinicio y su reversa corren en una unidad de systemd aparte, que sobrevive al corte; su resultado queda en el
  # journal (journalctl -u goagenda-reinicio-tunel).
  REINICIO="systemctl restart cloudflared; sleep 5; systemctl is-active --quiet cloudflared || { cp '$CF_CONFIG.bak.$ESTAMPA' '$CF_CONFIG'; systemctl restart cloudflared; echo 'cloudflared no arranco con el config nuevo: restaurado el anterior'; exit 1; }; echo 'tunel reiniciado con $HOST_API'"
  if command -v systemd-run >/dev/null; then
    $SUDO systemd-run --quiet --unit="goagenda-reinicio-tunel-$ESTAMPA" bash -c "$REINICIO" \
      && ok "reinicio del tunel programado (puede cortar esta sesion SSH: reconecta y revisa: journalctl -u goagenda-reinicio-tunel-$ESTAMPA)" \
      || falla "no se pudo programar el reinicio del tunel; el config nuevo ya esta puesto (restauralo con --revertir)."
  else
    $SUDO bash -c "$REINICIO" && ok "tunel reiniciado con $HOST_API"
  fi
fi

# ---------------------------------------------------------------- resumen
echo
echo "Comprobacion local (nginx por nombre de host):"
curl -s -o /dev/null -w "  $HOST_FRONT  -> HTTP %{http_code}\n" -H "Host: $HOST_FRONT" http://127.0.0.1/ || true
curl -s -o /dev/null -w "  $HOST_API -> HTTP %{http_code}\n" -H "Host: $HOST_API" http://127.0.0.1/ || true
echo
echo "Pendiente tuyo en Cloudflare (DNS): registro CNAME  api-pruebas  ->  <id-del-tunel>.cfargotunnel.com  (con proxy)."
echo "                                    (pruebas ya existe)"
[ "$API_LISTA" = 1 ] || echo "Pendiente: crear el Supabase de pruebas y $DIR_PRUEBAS/.env, y volver a correr este script."
