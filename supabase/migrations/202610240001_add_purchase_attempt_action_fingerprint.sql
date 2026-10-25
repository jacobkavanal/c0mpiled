-- Record canonical controller fingerprints for hard-denied purchase attempts.
-- NULL remains valid for historical rows and non-denial audit records.
ALTER TABLE public.purchase_attempts
    ADD COLUMN IF NOT EXISTS action_fingerprint text;

ALTER TABLE public.purchase_attempts
    DROP CONSTRAINT IF EXISTS purchase_attempts_action_fingerprint_format;

ALTER TABLE public.purchase_attempts
    ADD CONSTRAINT purchase_attempts_action_fingerprint_format
    CHECK (
        action_fingerprint IS NULL
        OR action_fingerprint ~ '^[0-9a-f]{64}$'
    );

CREATE INDEX IF NOT EXISTS purchase_attempts_denied_action_fingerprint_idx
    ON public.purchase_attempts (action_fingerprint)
    WHERE action_fingerprint IS NOT NULL AND decision = 'block';
