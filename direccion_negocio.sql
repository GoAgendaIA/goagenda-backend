-- Dirección del negocio (se muestra al cliente, p. ej. en el mensaje de confirmación de la cita).
--
-- Correr una sola vez en el SQL Editor de Supabase, ANTES de desplegar el backend que la usa.
-- Es texto libre a propósito: no se impone formato (sin '#', guiones ni números consecutivos)
-- y no depende de que Google Maps la conozca. Los negocios que ya existen quedan sin dirección
-- (null) y no se bloquean; solo los que terminen el onboarding de aquí en adelante la necesitan.
--
--   address        la dirección tal como la escribe el dueño (nomenclatura o referencias)
--   address_kind   'formal' (calle/carrera/número...) o 'referencias' (vereda, finca, "después del puente...")
--   address_city   municipio/barrio/vereda, opcional
--   address_notes  indicaciones extra para llegar (opcional)
--   address_lat/lng  coordenadas del pin del mapa, opcionales (no se exigen para direcciones por texto)

ALTER TABLE businesses ADD COLUMN IF NOT EXISTS address text;
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS address_kind text NOT NULL DEFAULT 'formal'
  CHECK (address_kind IN ('formal', 'referencias'));
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS address_city text;
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS address_notes text;
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS address_lat double precision;
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS address_lng double precision;
