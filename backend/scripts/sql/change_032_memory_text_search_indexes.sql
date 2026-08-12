-- Add trigram-backed text search indexes for app-owned memory recall.
--
-- Versioned memory_events remains the source of truth. These indexes are a
-- projection for faster lexical recall over value, subject, and JSON metadata.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_memory_events_value_trgm
ON public.memory_events
USING gin (value gin_trgm_ops)
WHERE is_deleted = FALSE AND superseded_by IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_events_subject_trgm
ON public.memory_events
USING gin (subject gin_trgm_ops)
WHERE is_deleted = FALSE AND superseded_by IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_events_metadata_text_trgm
ON public.memory_events
USING gin ((metadata::text) gin_trgm_ops)
WHERE is_deleted = FALSE AND superseded_by IS NULL;

ANALYZE public.memory_events;
