#!/usr/bin/env bash
#
# P2 · Clona el ESQUEMA (sin datos) de un proyecto de Supabase a otro vacio. Asi pruebas y produccion son iguales sin copiar
# datos de clientes. Los .sql del repo NO bastan: el esquema base (appointments, businesses, services...) no esta en ellos.
# Orden de los .sql y procedimiento: docs/ORDEN_MIGRACIONES.md.
#
#   ORIGEN_URL=<session pooler origen> DESTINO_URL=<session pooler destino> bash scripts/clonar_esquema_supabase.sh
#
# Protecciones: ambas URL deben ser el Session pooler (puerto 5432) y distintas, y el destino debe estar vacio (sin tablas
# en public). Todo se aplica en UNA transaccion: si algo falla, no queda nada a medias.

set -Eeuo pipefail

ORIGEN_URL="${ORIGEN_URL:-}"; DESTINO_URL="${DESTINO_URL:-}"
[ -n "$ORIGEN_URL" ] && [ -n "$DESTINO_URL" ] || { echo "✗ Indica ORIGEN_URL y DESTINO_URL"; exit 1; }
[ "$ORIGEN_URL" != "$DESTINO_URL" ] || { echo "✗ Origen y destino son iguales"; exit 1; }
for u in "$ORIGEN_URL" "$DESTINO_URL"; do
  case "$u" in *:5432/*) ;; *) echo "✗ Usa el Session pooler (puerto 5432)"; exit 1;; esac
  case "$u" in *"[YOUR-PASSWORD]"*) echo "✗ Falta reemplazar [YOUR-PASSWORD]"; exit 1;; esac
done
command -v pg_dump >/dev/null && command -v psql >/dev/null || { echo "✗ Faltan pg_dump/psql (macOS: brew install libpq)"; exit 1; }

EXISTENTES="$(psql "$DESTINO_URL" -Atc "select count(*) from information_schema.tables where table_schema='public'")"
[ "$EXISTENTES" = "0" ] || { echo "✗ El destino ya tiene $EXISTENTES tablas en public: no se toca"; exit 1; }

TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
pg_dump "$ORIGEN_URL" --schema-only --schema=public --no-owner --no-comments \
  | grep -v -e '^CREATE SCHEMA public;$' -e '^ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin ' > "$TMP"
# Los dos filtros: `public` ya existe en todo proyecto de Supabase, y el usuario `postgres` no puede cambiar los permisos
# por defecto de `supabase_admin` (que un proyecto nuevo ya trae iguales).
grep -q -E '^(COPY|INSERT INTO)' "$TMP" && { echo "✗ El volcado trae datos; abortado"; exit 1; }

psql "$DESTINO_URL" -v ON_ERROR_STOP=1 -1 -q -f "$TMP" >/dev/null
echo "✓ Esquema clonado:"
psql "$DESTINO_URL" -Atc "select '  tablas: '||count(*) from information_schema.tables where table_schema='public'"
psql "$DESTINO_URL" -Atc "select '  politicas: '||count(*) from pg_policies where schemaname='public'"
