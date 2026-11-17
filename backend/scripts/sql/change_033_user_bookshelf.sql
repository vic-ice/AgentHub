-- change_033_user_bookshelf.sql
-- Current reading shelf (authoritative current status) + backfill marker.
-- Shelf rows are written only by ReadingService; recommendation_events and
-- book_interactions remain the historical audit trail.

CREATE TABLE IF NOT EXISTS public.user_book_shelf (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    book_id           UUID REFERENCES public.books(id) ON DELETE SET NULL,
    title             VARCHAR(256) NOT NULL,
    authors           JSONB NOT NULL DEFAULT '[]'::jsonb,
    tags              JSONB NOT NULL DEFAULT '[]'::jsonb,
    cover_url         VARCHAR(1024),
    source_url        VARCHAR(1024),
    reading_status    VARCHAR(32) NOT NULL DEFAULT 'want_to_read',
    evaluation        VARCHAR(32),
    note              TEXT NOT NULL DEFAULT '',
    rating            INTEGER,
    last_event_at     TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_user_book_shelf_status CHECK (
        reading_status IN ('want_to_read', 'reading', 'read', 'dropped')
    ),
    CONSTRAINT chk_user_book_shelf_evaluation CHECK (
        evaluation IS NULL OR evaluation IN ('liked', 'disliked', 'not_interested')
    ),
    CONSTRAINT chk_user_book_shelf_rating CHECK (
        rating IS NULL OR (rating >= 1 AND rating <= 5)
    ),
    CONSTRAINT uq_user_book_shelf_user_book UNIQUE (user_id, book_id)
);

CREATE INDEX IF NOT EXISTS idx_user_book_shelf_user_status
ON public.user_book_shelf(user_id, reading_status, last_event_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_user_book_shelf_user_evaluation
ON public.user_book_shelf(user_id, evaluation)
WHERE evaluation IS NOT NULL;

-- One row per user marks that shelf backfill from history has been applied,
-- so a fully-emptied shelf is never re-created from old audit events.
CREATE TABLE IF NOT EXISTS public.user_bookshelf_state (
    user_id        UUID PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    initialized_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Extend recommendation event type check with reading + dropped statuses.
ALTER TABLE public.recommendation_events
    DROP CONSTRAINT IF EXISTS chk_recommendation_events_type;

ALTER TABLE public.recommendation_events
    ADD CONSTRAINT chk_recommendation_events_type CHECK (
        event_type IN (
            'candidate_retrieved',
            'recommended',
            'followup_suggested',
            'followup_clicked',
            'followup_matched',
            'detail_requested',
            'want_to_read',
            'reading',
            'read',
            'dropped',
            'liked',
            'disliked',
            'not_interested',
            'suppressed'
        )
    );

ANALYZE public.user_book_shelf;
ANALYZE public.user_bookshelf_state;