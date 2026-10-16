-- Aviso de primera vez del panel «Estadisticas» (Trello: «Implementar estadisticas por negocio»).
--
-- Guarda, por usuario, si ya vio el aviso «Ahora podras tomar el control de tu negocio con el
-- panel de estadisticas». Queda en false para todos: cada dueño lo ve UNA vez despues de que
-- esta funcion se despliegue. Se guarda en el servidor para que no vuelva a salir en otro
-- dispositivo o navegador.
--
-- Correr una sola vez en el SQL Editor de Supabase. Es seguro correrlo antes o despues de
-- desplegar: sin la columna el aviso simplemente no se muestra (la app lo trata como visto).

ALTER TABLE employees ADD COLUMN IF NOT EXISTS stats_intro_seen boolean NOT NULL DEFAULT false;
