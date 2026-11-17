-- change_031_allow_admin_memory_sources.sql
-- Memory version provenance is validated in code by MemoryVersionStore
-- (_require_user_source: conversation_events then memory_management_events).
-- The single-table FK to conversation_events cannot express that dual
-- source, so it is dropped; the ownership + evidence checks remain enforced
-- in the write path.

ALTER TABLE public.memory_events DROP CONSTRAINT IF EXISTS fk_memory_events_source_event;
