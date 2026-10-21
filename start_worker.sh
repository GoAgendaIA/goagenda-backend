#!/bin/bash
# Arranca el scheduler (encola) y el worker (ejecuta) en el MISMO contenedor. Si cualquiera muere, se
# apaga el otro y el contenedor termina con error para que la plataforma lo reinicie: un worker sin
# scheduler (o al reves) seria un fallo silencioso (los recordatorios dejarian de salir sin avisar).
set -u

python scheduler.py &
SCHEDULER_PID=$!
dramatiq worker --processes 1 --threads 4 &
WORKER_PID=$!

detener() {
  kill "$SCHEDULER_PID" "$WORKER_PID" 2>/dev/null
  wait "$SCHEDULER_PID" "$WORKER_PID" 2>/dev/null
}
# Apagado ordenado (docker stop / redeploy): no es una falla.
trap 'detener; exit 0' TERM INT

# Vigila ambos procesos (sin `wait -n`: no existe en el bash 3.2 de macOS y asi tambien se puede probar ahi).
while kill -0 "$SCHEDULER_PID" 2>/dev/null && kill -0 "$WORKER_PID" 2>/dev/null; do
  sleep 1
done

if ! kill -0 "$SCHEDULER_PID" 2>/dev/null; then
  wait "$SCHEDULER_PID"; CODIGO=$?; QUIEN="scheduler"
else
  wait "$WORKER_PID"; CODIGO=$?; QUIEN="worker"
fi
echo "[start_worker] el $QUIEN termino (codigo $CODIGO): se apaga el contenedor para que la plataforma lo reinicie" >&2
detener
exit "$((CODIGO == 0 ? 1 : CODIGO))"
