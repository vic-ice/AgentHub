-- Expand RecommendationEvent provenance to the same cross-domain source contract
-- accepted by MemoryWriteGateway. This changes only origin classification;
-- ReadingService continues to own event meaning and state transitions.

ALTER TABLE public.recommendation_events
    DROP CONSTRAINT IF EXISTS chk_recommendation_signal_source;

ALTER TABLE public.recommendation_events
    ADD CONSTRAINT chk_recommendation_signal_source CHECK (
        source IN (
            'agent_tool',
            'api',
            'book_feedback',
            'followup_question',
            'system',
            'user_message',
            'reading_event',
            'tool_execution',
            'admin_action',
            'correction',
            'system_derived'
        )
    );

ANALYZE public.recommendation_events;
