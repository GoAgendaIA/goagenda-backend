-- Ejecutar en Supabase: Dashboard > SQL Editor > New query > pegar y Run.
-- DEBE correrse ANTES de desplegar el codigo que lo usa: create_appointment
-- ahora envia la columna `source` en cada insert.
--
-- Instrumentacion para el Centro de metricas y analitica (ver
-- docs/METRICAS_ANALITICA.md y services/metrics.py).

-- ---------------------------------------------------------------------
-- 1) appointments: origen, quien cancelo y reprogramaciones
-- ---------------------------------------------------------------------
alter table appointments add column if not exists created_at timestamptz not null default now();

-- Origen de la cita. 'ai' = agendada por el asistente (chat), 'manual' =
-- creada por el negocio desde el panel. 'unknown' = citas anteriores a esta
-- migracion: no se pueden clasificar con certeza, asi que NO se asumen.
alter table appointments add column if not exists source text not null default 'unknown';
alter table appointments drop constraint if exists appointments_source_check;
alter table appointments add constraint appointments_source_check
  check (source in ('ai', 'manual', 'web', 'unknown'));

-- Quien cancelo: 'ai' (el cliente via el asistente), 'system' (el link de
-- pago vencio) o NULL (cancelada desde el panel / sin dato).
alter table appointments add column if not exists cancelled_by text;

-- Reprogramar mueve la MISMA fila (no crea una cita nueva). Estos
-- contadores dejan rastro: cuantas veces se movio y cuantas por la IA.
alter table appointments add column if not exists rescheduled_count integer not null default 0;
alter table appointments add column if not exists rescheduled_by_ai_count integer not null default 0;
alter table appointments add column if not exists last_rescheduled_at timestamptz;
alter table appointments add column if not exists cancelled_at timestamptz;
alter table appointments add column if not exists completed_at timestamptz;

-- Un trigger (y no el codigo Python) llena las marcas de tiempo y el
-- contador, para que tambien queden registradas las ediciones que el panel
-- hace directo contra Supabase y que el backend nunca ve.
create or replace function appointments_registrar_cambios() returns trigger as $$
begin
  if new.status is distinct from old.status then
    if new.status = 'cancelled' and new.cancelled_at is null then
      new.cancelled_at := now();
    elsif new.status = 'completed' and new.completed_at is null then
      new.completed_at := now();
    end if;
  end if;
  if new.scheduled_at is distinct from old.scheduled_at then
    new.rescheduled_count := coalesce(old.rescheduled_count, 0) + 1;
    new.last_rescheduled_at := now();
  end if;
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_appointments_registrar_cambios on appointments;
create trigger trg_appointments_registrar_cambios
  before update on appointments
  for each row execute function appointments_registrar_cambios();

create index if not exists appointments_business_created_idx on appointments (business_id, created_at);
create index if not exists appointments_business_scheduled_idx on appointments (business_id, scheduled_at);

-- ---------------------------------------------------------------------
-- 2) chat_sessions: una fila por conversacion del chat publico
-- ---------------------------------------------------------------------
-- Antes las conversaciones solo vivian en los checkpoints de LangGraph (no
-- consultables). Esta tabla las hace medibles: cuantas se iniciaron, cuantas
-- terminaron en cita y cuantas necesitaron a un humano.
create table if not exists chat_sessions (
  business_id uuid not null references businesses(id) on delete cascade,
  session_id text not null,
  started_at timestamptz not null default now(),
  last_message_at timestamptz not null default now(),
  message_count integer not null default 0,
  ended_in_appointment boolean not null default false,
  appointment_id uuid,
  handed_off boolean not null default false,
  primary key (business_id, session_id)
);

create index if not exists chat_sessions_business_started_idx on chat_sessions (business_id, started_at);

-- RLS activado sin policies: solo el backend (service role) la toca.
alter table chat_sessions enable row level security;
