-- 005 · AI layer: ground truth, quarantine and evaluation results.
--
-- Three tables that together make the AI auditable rather than merely present:
--
--   review_aspect_truth  the known aspects that were composed into each review.
--                        The evaluation gold standard. GENERATOR ground truth, NOT
--                        human annotation -- recorded here so the distinction
--                        cannot be quietly lost.
--   absa_quarantine      extractions that failed validation and were BLOCKED from
--                        reaching any dashboard. Kept, not discarded: the
--                        quarantine rate is a headline number and the contents are
--                        the evidence that validation does something.
--   ai_eval_result       per-run, per-method, per-category scores. Persisted so a
--                        benchmark can be re-read rather than re-trusted.
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS meta.review_aspect_truth (
    truth_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_id     text    NOT NULL,
    category      text    NOT NULL,
    polarity      text    NOT NULL CHECK (polarity IN ('positive','negative','neutral')),
    severity      text    NOT NULL,
    actionable_by text    NOT NULL,
    UNIQUE (review_id, category, polarity)
);

CREATE INDEX IF NOT EXISTS ix_truth_review ON meta.review_aspect_truth (review_id);

COMMENT ON TABLE meta.review_aspect_truth IS
    'Aspect labels known to have been composed into each generated review. Used as '
    'the evaluation gold set. This is GENERATOR ground truth: it measures whether '
    'the model recovers injected aspects, not whether it agrees with a human.';


CREATE TABLE IF NOT EXISTS meta.absa_quarantine (
    quarantine_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_id     text        NOT NULL,
    model         text        NOT NULL,
    category      text,
    polarity      text,
    evidence_span text,
    reason        text        NOT NULL,
    raw_payload   jsonb,
    quarantined_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_quarantine_reason ON meta.absa_quarantine (reason);

COMMENT ON TABLE meta.absa_quarantine IS
    'Model output that failed validation and never reached a dashboard. Reasons '
    'include evidence_span not being a literal substring of the source review '
    '(a fabricated quote), an unknown enum value, or a malformed response.';


CREATE TABLE IF NOT EXISTS meta.ai_eval_result (
    eval_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_at        timestamptz NOT NULL DEFAULT now(),
    method        text    NOT NULL,          -- 'gemini' | 'keyword_baseline'
    model         text,
    scope         text    NOT NULL,          -- 'overall' | 'category' | 'language'
    scope_value   text,
    n_gold        integer NOT NULL,
    true_positive integer NOT NULL,
    false_positive integer NOT NULL,
    false_negative integer NOT NULL,
    precision_pct numeric(6,2),
    recall_pct    numeric(6,2),
    f1_pct        numeric(6,2),
    notes         text
);

CREATE INDEX IF NOT EXISTS ix_eval_method_scope ON meta.ai_eval_result (method, scope, run_at DESC);


-- Reviews that carry an operational problem despite a high rating. This is the
-- artifact that justifies aspect extraction over document-level sentiment: a
-- 5-star review reporting a two-hour housekeeping wait is a work item, and a
-- sentiment score throws it away.
CREATE OR REPLACE VIEW mart.v_buried_complaints AS
SELECT
    r.review_id,
    p.property_code,
    r.review_date,
    r.rating,
    r.language,
    a.category,
    a.severity,
    a.actionable_by,
    a.evidence_span,
    a.confidence,
    r.review_text
FROM mart.fact_review_aspect a
JOIN mart.fact_review   r ON r.review_key    = a.review_key
JOIN mart.dim_property  p ON p.property_key  = a.property_key
WHERE a.polarity = 'negative'
  AND a.evidence_verified
  AND r.rating >= 4.0
ORDER BY
    CASE a.severity WHEN 'severe' THEN 1 WHEN 'moderate' THEN 2 ELSE 3 END,
    r.review_date DESC;

COMMENT ON VIEW mart.v_buried_complaints IS
    'Negative aspects inside 4-and-5-star reviews. Only evidence-verified rows '
    'appear. On a 4.8-star corpus a sentiment classifier returns ~96% positive and '
    'surfaces none of these.';
