#!/usr/bin/env bash
#
# Cambia el modelo de OpenAI del chat en el VPS, lo verifica con una conversacion de prueba y, si
# algo falla, vuelve SOLO al .env anterior.
#
# Uso (en el VPS):
#   BUSINESS_ID=<id-de-un-negocio-con-chat-activo> ./cambiar_modelo_vps.sh gpt-6-luna
#
# Variables opcionales:
#   DIR      carpeta del docker-compose del backend   (default /opt/goagenda/backend)
#   API_URL  URL local de la API                       (default http://localhost:8000)
#
# Para volver al modelo anterior a mano:  cp .env.bak.<fecha> .env && docker compose up -d --force-recreate

set -Eeuo pipefail

MODELO="${1:?Uso: BUSINESS_ID=<id> $0 <modelo>   (ej. gpt-6-luna)}"
BUSINESS_ID="${BUSINESS_ID:?Falta BUSINESS_ID: el id de un negocio con el chat activo, para la prueba}"
DIR="${DIR:-/opt/goagenda/backend}"
API_URL="${API_URL:-http://localhost:8000}"

cd "$DIR"
[ -f .env ] || { echo "No existe $DIR/.env"; exit 1; }

COPIA=".env.bak.$(date +%Y%m%d-%H%M%S)"
cp .env "$COPIA" && chmod 600 "$COPIA"
echo "Copia de seguridad: $DIR/$COPIA"

# poner CLAVE VALOR: reemplaza la linea si existe, la agrega si no (nunca imprime el contenido del .env)
poner() {
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}

volver_atras() {
  echo
  echo "✗ $1"
  echo "Restaurando el .env anterior y reiniciando..."
  cp "$COPIA" .env
  docker compose up -d --force-recreate >/dev/null
  echo "Listo: el chat volvió a la configuración anterior."
  exit 1
}

# 1) Variables. Los modelos de razonamiento (gpt-5.x, gpt-6.x, o-series) usan un tope de salida mayor
#    (incluye el razonamiento oculto) y se les limita cuanto razonan; el codigo ya omite temperature.
poner CHAT_MODEL_NAME "$MODELO"
if [[ "$MODELO" =~ ^(gpt-5|gpt-6|o1|o3|o4) ]]; then
  poner LLM_MAX_TOKENS_RAZONAMIENTO 4000
  poner LLM_REASONING_EFFORT low
  echo "Modelo de razonamiento: se configuran LLM_MAX_TOKENS_RAZONAMIENTO=4000 y LLM_REASONING_EFFORT=low"
fi

# 2) Imagen al dia (trae el codigo que sabe usar modelos de razonamiento) y reinicio.
for intento in 1 2 3; do
  docker compose pull && break
  [ "$intento" = 3 ] && volver_atras "No se pudo descargar la imagen."
  echo "pull fallo (intento $intento/3), reintentando en 15 s..."; sleep 15
done
docker compose up -d --force-recreate >/dev/null

# 3) Esperar a que la API responda.
echo -n "Esperando a la API"
for _ in $(seq 1 45); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$API_URL/chat/$BUSINESS_ID/config" || true)" = "200" ] && break
  echo -n "."; sleep 2
done
echo
[ "$(curl -s -o /dev/null -w '%{http_code}' "$API_URL/chat/$BUSINESS_ID/config" || true)" = "200" ] \
  || volver_atras "La API no respondió en $API_URL (¿puerto o BUSINESS_ID incorrectos?)."

# 4) Conversacion de prueba: un saludo debe responder con la lista REAL de servicios (con precios),
#    lo que prueba que el modelo acepta los parametros y llama a las herramientas.
json() { python3 -c "import sys,json; print(json.load(sys.stdin).get('$1',''))"; }
SESION="$(curl -s -X POST "$API_URL/chat/$BUSINESS_ID/sessions" | json session_id)"
[ -n "$SESION" ] || volver_atras "No se pudo crear la sesión de prueba."
RESPUESTA="$(curl -s -m 90 -X POST "$API_URL/chat/$BUSINESS_ID/sessions/$SESION/messages" \
  -H 'Content-Type: application/json' -d '{"mensaje":"Hola, ¿qué servicios tienen y cuánto cuestan?"}' | json respuesta)"

echo "--- Respuesta del chat con $MODELO ---"
echo "$RESPUESTA"
echo "--------------------------------------"

[ -n "$RESPUESTA" ] || volver_atras "La respuesta llegó vacía."
echo "$RESPUESTA" | grep -qiE "problema t.{1,2}cnico" && volver_atras "El chat respondió con el error técnico: el modelo rechazó la petición."
echo "$RESPUESTA" | grep -q '\$' || volver_atras "La respuesta no trae precios: el modelo no consultó los servicios."

echo
echo "✓ Listo: el chat usa $MODELO y respondió bien."
echo "  Falta tu prueba manual del flujo completo (servicio → día → horas disponibles → agendar)."
echo "  Para volver atrás:  cd $DIR && cp $COPIA .env && docker compose up -d --force-recreate"
