-- change_034_shelf_neutral_evaluation.sql
-- Add the "neutral" (一般) evaluation level to the current shelf.
-- Book-level disliked/not_interested remain negative samples (soft down-rank of
-- similar books); only explicit style-level dislikes become avoid preferences.

ALTER TABLE public.user_book_shelf
    DROP CONSTRAINT IF EXISTS chk_user_book_shelf_evaluation;

ALTER TABLE public.user_book_shelf
    ADD CONSTRAINT chk_user_book_shelf_evaluation CHECK (
        evaluation IS NULL OR evaluation IN ('liked', 'neutral', 'disliked', 'not_interested')
    );

ANALYZE public.user_book_shelf;