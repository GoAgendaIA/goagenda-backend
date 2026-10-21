#!/usr/bin/env bash
#
# P1 · Centralizar repos en la organizacion GoAgendaIA.
# Cambia los docker-compose del VPS para que usen las imagenes de la organizacion
#   ghcr.io/contactogoagenda-eng/goagenda-backend -> ghcr.io/goagendaia/goagenda-backend
#   ghcr.io/duban0909/go-front                    -> ghcr.io/goagendaia/go-front
#
# Correr como el usuario dueño de /opt/goagenda (deploy), DESPUES de que el primer push a main en
# la organizacion haya publicado las dos imagenes y estas sean publicas.
#
#   ./migrar_imagenes_organizacion.sh            # aplica
#   SOLO_VERIFICAR=1 ./migrar_imagenes_organizacion.sh   # solo comprueba que las imagenes existan
#
# Antes de tocar nada comprueba que las imagenes nuevas existan y se puedan descargar sin token;
# si un paso falla, restaura el docker-compose anterior de ese servicio.

set -Eeuo pipefail

ORG="${ORG:-goagendaia}"
BASE="${BASE:-/opt/goagenda}"

# carpeta | imagen vieja | imagen nueva
SERVICIOS=(
  "$BASE/backend|ghcr.io/contactogoagenda-eng/goagenda-backend|ghcr.io/$ORG/goagenda-backend"
  "$BASE/frontend|ghcr.io/duban0909/go-front|ghcr.io/$ORG/go-front"
)

archivo_compose() {
  for f in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do
    [ -f "$1/$f" ] && { echo "$1/$f"; return 0; }
  done
  return 1
}

# true si la imagen (ghcr.io/<owner>/<repo>) tiene :latest y se puede descargar SIN credenciales:
# pide un token anonimo a GHCR y consulta el manifiesto, igual que haria un `docker pull` anonimo.
# (`docker manifest inspect` no sirve aqui: puede responder "denied" aunque la imagen sea publica.)
imagen_publica() {
  local ruta="${1#ghcr.io/}" token
  token="$(curl -fsS "https://ghcr.io/token?scope=repository:${ruta}:pull&service=ghcr.io" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)" || return 1
  [ -n "$token" ] || return 1
  [ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" \
      -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json' \
      "https://ghcr.io/v2/${ruta}/manifests/latest")" = "200" ]
}

# 1) Las imagenes nuevas existen y son publicas (se consultan sin credenciales).
echo "== Comprobando las imagenes de la organizacion =="
for s in "${SERVICIOS[@]}"; do
  IFS='|' read -r _ _ nueva <<< "$s"
  if imagen_publica "$nueva"; then
    echo "  ✓ $nueva:latest (publica)"
  else
    echo "  ✗ $nueva:latest no existe o no es publica."
    echo "    Haz un push a main en ese repo (ya en la organizacion) y marca el paquete como publico en"
    echo "    GitHub > GoAgendaIA > Packages > <paquete> > Package settings > Change visibility."
    exit 1
  fi
done
[ "${SOLO_VERIFICAR:-0}" = "1" ] && { echo "Solo verificacion: no se cambio nada."; exit 0; }

# 2) Cambiar cada servicio, con copia y reversa.
for s in "${SERVICIOS[@]}"; do
  IFS='|' read -r dir vieja nueva <<< "$s"
  compose="$(archivo_compose "$dir")" || { echo "No hay docker-compose en $dir"; exit 1; }
  echo
  echo "== $dir =="
  if ! grep -q "$vieja" "$compose"; then
    if grep -q "$nueva" "$compose"; then echo "  ya usa $nueva: nada que cambiar"; continue; fi
    echo "  ✗ $compose no menciona $vieja ni $nueva: revisalo a mano."; exit 1
  fi

  copia="$compose.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$compose" "$copia"
  sed -i "s|$vieja|$nueva|g" "$compose"
  echo "  imagen: $vieja -> $nueva (copia: $copia)"

  if ! (cd "$dir" && docker compose pull && docker compose up -d --remove-orphans); then
    echo "  ✗ Fallo el pull o el arranque: restaurando el compose anterior"
    cp "$copia" "$compose"
    (cd "$dir" && docker compose up -d --remove-orphans) || true
    exit 1
  fi

  sleep 5
  if [ -z "$(cd "$dir" && docker compose ps --status running -q)" ]; then
    echo "  ✗ El contenedor no quedo corriendo: restaurando el compose anterior"
    cp "$copia" "$compose"
    (cd "$dir" && docker compose up -d --remove-orphans) || true
    exit 1
  fi
  echo "  ✓ corriendo con $nueva"
done

docker image prune -f >/dev/null || true
echo
echo "✓ Listo: el VPS usa las imagenes de la organizacion. Los proximos deploys las actualizan solos."
