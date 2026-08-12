-- change_030_memory_management_events.sql
-- Append-only source ledger for user-initiated memory edits/forgets.

CREATE TABLE IF NOT EXISTS public.memory_management_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    thread_id       UUID REFERENCES public.conversations(thread_id) ON DELETE SET NULL,
    action          VARCHAR(16) NOT NULL,
    schema_key      VARCHAR(128),
    memory_key      VARCHAR(256),
    subject         VARCHAR(64) NOT NULL DEFAULT 'self',
    predicate       VARCHAR(256) NOT NULL,
    value           JSONB NOT NULL DEFAULT '{}'::jsonb,
    qualifiers      JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_quote  TEXT NOT NULL,
    previous_value  JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_management_user_created
ON public.memory_management_events(user_id, created_at DESC);
