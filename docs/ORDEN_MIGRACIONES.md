# Orden de los `.sql` y cómo clonar el esquema

Los `.sql` de la raíz son parches que se corrieron a mano en el SQL Editor de Supabase. **No describen el esquema completo**:
las tablas base (`appointments`, `businesses`, `services`, `employees`, `ai_usage_log`…) nacieron antes y no están en el repo.
Por eso un ambiente nuevo **no se crea corriendo los `.sql`**, sino clonando el esquema del proyecto de producción.

## Ambientes

| Ambiente | Proyecto de Supabase | Datos |
|---|---|---|
| Producción | el actual (`ghwkiuwcovrmpztamjgz`, us-east-2) | reales (Elegant Barber). **No se migra ni se toca.** |
| Pruebas | `mivhmjztunqgawdwoyyb` (us-east-1) | ninguno de clientes: esquema clonado + un negocio de ejemplo |

Decisión (2026-10-21): el proyecto actual **sigue siendo producción**. Así no se mueven cuentas de Auth ni datos de clientes.

## Clonar el esquema a un proyecto vacío

```bash
brew install libpq   # pg_dump/psql >= la versión del servidor (Postgres 17)
export PATH=/opt/homebrew/opt/libpq/bin:$PATH
ORIGEN_URL='<Session pooler de producción>' DESTINO_URL='<Session pooler del destino>' \
  bash scripts/clonar_esquema_supabase.sh
```

El script exige el Session pooler (puerto 5432), un destino vacío y aplica todo en una sola transacción. Quita dos cosas
del volcado que Supabase no permite al usuario `postgres`: `CREATE SCHEMA public` y los
`ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin`. Resultado esperado hoy: 23 tablas, 15 políticas, 1 trigger.

Después, en el proyecto nuevo (no se copian del volcado):
1. **Auth → URL Configuration**: Site URL y Redirect URLs del ambiente (`https://pruebas.goagenda.online`).
2. **Authentication → Email**: *Confirm email* igual que producción; SMTP propio si se necesita más de unos pocos correos por hora.
3. El **super admin** lo crea la API al arrancar con `SUPER_ADMIN_EMAIL`/`SUPER_ADMIN_PASSWORD` en su `.env`.
4. Storage: el bucket `assets` existe en producción pero el código no lo usa; no hace falta recrearlo.
5. No hay Database Webhook ni `pg_cron`: el único trigger de `public` es `trg_appointments_registrar_cambios` (viene en el volcado).

## Orden histórico de los `.sql` (por fecha de alta en git)

Solo sirve para entender qué se aplicó y cuándo; **un parche nuevo se corre en producción y se añade aquí**, y el esquema de
pruebas se rehace o se parchea con el mismo archivo. Casi todos son idempotentes.

| # | Archivo | Alta |
|---|---|---|
| 1 | `excluded_chats.sql` | 2026-07-20 |
| 2 | `roles_empleados_invitaciones.sql` | 2026-08-27 |
| 3 | `whatsapp_client_contacts.sql` | 2026-09-03 |
| 4 | `onboarding.sql` | 2026-09-10 |
| 5 | `domicilios.sql` | 2026-09-20 |
| 6 | `appointments_pending_payment.sql` | 2026-09-22 |
| 7 | `home_visit_zones_aliases.sql` | 2026-09-22 |
| 8 | `wompi_pagos.sql` | 2026-09-22 |
| 9 | `owner_employee_hours_sync.sql` | 2026-10-08 |
| 10 | `metricas_analitica.sql` (debe correr **antes** de desplegar el código que inserta `appointments.source`) | 2026-10-13 |
| 11 | `tokens_uso_ia.sql` | 2026-10-13 |
| 12 | `direccion_negocio.sql` | 2026-10-14 |
| 13 | `estadisticas_intro.sql` | 2026-10-15 |

## Copia de seguridad de producción

El plan gratis de Supabase no incluye backups. Antes de cualquier corte:

```bash
DESTINO=~/respaldos-goagenda bash scripts/respaldo_produccion.sh
```

Guarda un `.dump` (esquema `public` + cuentas de `auth`) con permisos 600 fuera del repo y verifica que sea legible. **Contiene
datos reales de clientes**: no lo subas a GitHub ni a una carpeta compartida.
