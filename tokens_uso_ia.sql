-- Ejecutar en Supabase: Dashboard > SQL Editor > New query > pegar y Run.
-- Opcional pero recomendado: instrumentacion de tokens (docs/ESTRATEGIA_OPTIMIZACION_TOKENS.md, T0.1).
-- El backend funciona sin esta migracion: si las columnas no existen, guarda el
-- registro en el formato anterior. Con ella, el Centro de metricas puede mostrar
-- tokens cacheados, llamadas por conversacion y costo por conversacion.

alter table ai_usage_log add column if not exists cached_tokens integer not null default 0;
alter table ai_usage_log add column if not exists call_type text;      -- 'turno' | 're_llamada_anti_alucinacion'
alter table ai_usage_log add column if not exists session_id text;     -- conversacion (chat_sessions.session_id)
alter table ai_usage_log add column if not exists model text;

create index if not exists ai_usage_log_business_created_idx on ai_usage_log (business_id, created_at);

-- Marca del aviso "asistente pausado por presupuesto" (se avisa al dueño una vez por mes).
alter table businesses add column if not exists ai_budget_cutoff_sent_month text;
