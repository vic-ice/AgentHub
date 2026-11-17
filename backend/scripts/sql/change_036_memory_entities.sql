-- change_036_memory_entities.sql
-- Stable entity registry: every entity fact binds a stable entity_id.
-- The main memory chain (domain/kind/admission/conflict/version) is unchanged;
-- this adds identity so referents resolve to ONE object instead of text.

CREATE TABLE IF NOT EXISTS public.memory_entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    entity_type     VARCHAR(32) NOT NULL,
    canonical_name  VARCHAR(256) NOT NULL,
    aliases         JSONB NOT NULL DEFAULT '[]'::jsonb,
    domain          VARCHAR(32) NOT NULL DEFAULT 'general',
    external_ref    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_memory_entities_user_type_name UNIQUE (user_id, entity_type, canonical_name),
    CONSTRAINT chk_memory_entities_type CHECK (
        entity_type IN ('book','person','pet','object','place','account','project','other')
    ),
    CONSTRAINT chk_memory_entities_domain CHECK (
        domain IN ('reading','personal','possession','relationship','plan','general')
    )
);

CREATE INDEX IF NOT EXISTS idx_memory_entities_user_type
ON public.memory_entities(user_id, entity_type, canonical_name);

CREATE INDEX IF NOT EXISTS idx_memory_entities_user_domain
ON public.memory_entities(user_id, domain);

-- Bind memory facts to the entity registry (nullable for non-entity facts).
ALTER TABLE public.memory_events
    ADD COLUMN IF NOT EXISTS entity_id UUID REFERENCES public.memory_entities(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_memory_events_entity
ON public.memory_events(user_id, entity_id)
WHERE entity_id IS NOT NULL;

ANALYZE public.memory_entities;
ANALYZE public.memory_events;