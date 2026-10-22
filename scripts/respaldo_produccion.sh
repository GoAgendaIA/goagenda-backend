#!/usr/bin/env bash
#
# P2 · Copia de seguridad COMPLETA (esquema + datos) del proyecto de Supabase de produccion, para guardarla FUERA de
# Supabase antes de cualquier corte (p. ej. el paso a Seenode). El plan gratis de Supabase no incluye backups.
#
# La copia contiene datos reales de clientes (nombres, telefonos, citas): se guarda con permisos 600 en el directorio que
# indiques, que NO debe estar dentro del repo ni en una carpeta sincronizada publicamente.
#
#   DESTINO=~/respaldos-goagenda bash scripts/respaldo_produccion.sh
#
# Lee DATABASE_URL del .env del repo (o de la variable de entorno). Necesita pg_dump >= la version del servidor
# (macOS: `brew install libpq` y poner /opt/homebrew/opt/libpq/bin en el PATH).

set -Eeuo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
DESTINO="${DESTINO:-}"
[ -n "$DESTINO" ] || { echo "✗ Indica DESTINO=<carpeta fuera del repo>"; exit 1; }
case "$(cd "$DESTINO" 2>/dev/null && pwd || echo "$DESTINO")" in "$RAIZ"|"$RAIZ"/*) echo "✗ DESTINO no puede estar dentro del repo ($RAIZ)"; exit 1;; esac

URL="${DATABASE_URL:-}"
[ -n "$URL" ] || [ ! -f "$RAIZ/.env" ] || URL="$(grep -E '^DATABASE_URL=' "$RAIZ/.env" | head -1 | cut -d= -f2- | sed -e "s/^[\"']//" -e "s/[\"']\$//")"
[ -n "$URL" ] || { echo "✗ No hay DATABASE_URL (variable de entorno o .env)"; exit 1; }
command -v pg_dump >/dev/null || { echo "✗ pg_dump no esta en el PATH"; exit 1; }

mkdir -p "$DESTINO"
chmod 700 "$DESTINO"
ARCHIVO="$DESTINO/goagenda-produccion-$(date +%Y%m%d-%H%M%S).dump"

# Formato custom (-Fc): comprimido y restaurable de forma selectiva con pg_restore. Se respaldan los esquemas de la app y
# las cuentas de Auth; el resto de esquemas los administra Supabase.
umask 077
pg_dump "$URL" -Fc --no-owner --schema=public --schema=auth -f "$ARCHIVO"

# Verifica que el archivo se pueda leer y que traiga las tablas de la app.
TABLAS="$(pg_restore -l "$ARCHIVO" | grep -c 'TABLE DATA' || true)"
[ "$TABLAS" -gt 0 ] || { echo "✗ El respaldo no contiene datos de tablas; no se confia en el archivo"; exit 1; }
echo "✓ Respaldo guardado: $ARCHIVO ($(du -h "$ARCHIVO" | cut -f1), $TABLAS tablas con datos)"
echo "  Cuentas de Auth incluidas. Guardalo cifrado o en un disco de confianza; contiene datos reales."
