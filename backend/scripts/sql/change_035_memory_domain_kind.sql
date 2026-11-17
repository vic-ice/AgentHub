-- change_035_memory_domain_kind.sql
-- Unified two-layer memory classification: domain (领域) + kind (事实类型).
-- Reading continues to reuse this same memory_events store; no separate
-- ReadingMemory table is created.

ALTER TABLE public.memory_events
    ADD COLUMN IF NOT EXISTS domain VARCHAR(32),
    ADD COLUMN IF NOT EXISTS kind VARCHAR(32);

-- Best-effort backfill for legacy rows (schema_key takes precedence; the
-- provider still derives on read when these remain NULL).
UPDATE public.memory_events
SET
    domain = CASE
        WHEN schema_key LIKE 'identity.%' THEN 'personal'
        WHEN schema_key LIKE 'possession.%' THEN 'possession'
        WHEN schema_key LIKE 'relationship.%' THEN 'relationship'
        WHEN schema_key LIKE 'instruction.%' THEN 'personal'
        WHEN schema_key LIKE 'plan.%' THEN 'plan'
        WHEN schema_key LIKE 'temporary.%' THEN 'personal'
        WHEN schema_key LIKE 'feedback.%'
             AND subject IN ('book','author','tag','theme','style','genre','mood','pacing','content')
            THEN 'reading'
        WHEN schema_key LIKE 'preference.%'
             AND subject IN ('book','author','tag','theme','style','genre','mood','pacing','content')
            THEN 'reading'
        WHEN type = 'reading_state' THEN 'reading'
        WHEN type IN ('feedback','preference','state')
             AND subject IN ('book','author','tag','theme','style','genre','mood','pacing','content')
            THEN 'reading'
        WHEN type = 'entity' AND subject IN ('pet','object') THEN 'possession'
        WHEN type = 'entity' AND subject = 'person' THEN 'relationship'
        WHEN type = 'entity' AND subject = 'entity' THEN 'general'
        WHEN type = 'instruction' THEN 'personal'
        ELSE 'personal'
    END,
    kind = CASE
        WHEN schema_key LIKE 'identity.%' THEN 'fact'
        WHEN schema_key LIKE 'possession.%' THEN 'fact'
        WHEN schema_key LIKE 'relationship.%' THEN 'fact'
        WHEN schema_key LIKE 'instruction.%' THEN 'agreement'
        WHEN schema_key LIKE 'plan.%' THEN 'fact'
        WHEN schema_key LIKE 'temporary.%' THEN 'state'
        WHEN schema_key LIKE 'feedback.%' THEN 'feedback'
        WHEN schema_key LIKE 'preference.%' THEN 'preference'
        WHEN type = 'reading_state' THEN 'state'
        WHEN type = 'feedback' THEN 'feedback'
        WHEN type = 'preference' THEN 'preference'
        WHEN type = 'entity' THEN 'fact'
        WHEN type = 'state' THEN 'state'
        WHEN type = 'instruction' THEN 'agreement'
        WHEN type = 'correction' THEN 'correction'
        ELSE 'fact'
    END
WHERE domain IS NULL OR kind IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_events_user_domain
ON public.memory_events(user_id, domain)
WHERE domain IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_events_user_kind
ON public.memory_events(user_id, kind)
WHERE kind IS NOT NULL;

ANALYZE public.memory_events;