-- 001 · Layered schemas and the metadata backbone.
--
-- Layering keeps the analytical contract explicit:
--   raw     - landing zone, as-ingested, defects preserved verbatim
--   staging - typed, standardised, defects flagged but not silently repaired
--   mart    - conformed star schema, the analytical source of truth
--   meta    - the system's own record of itself: migrations, runs, quality,
--             metric definitions and lineage
--
-- Idempotent: safe to re-run.

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS meta;

COMMENT ON SCHEMA raw     IS 'Landing zone. As-ingested source extracts, defects intact.';
COMMENT ON SCHEMA staging IS 'Typed and standardised. Defects flagged, never silently repaired.';
COMMENT ON SCHEMA mart    IS 'Conformed star schema. The analytical source of truth.';
COMMENT ON SCHEMA meta    IS 'System self-knowledge: migrations, runs, data quality, metric definitions.';


-- ---------------------------------------------------------------------------
-- Migration ledger
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.schema_migration (
    filename        text        PRIMARY KEY,
    checksum_sha256 text        NOT NULL,
    applied_at      timestamptz NOT NULL DEFAULT now(),
    duration_ms     integer
);

COMMENT ON TABLE meta.schema_migration IS
    'Applied migrations. Checksum detects a migration edited after it was applied.';


-- ---------------------------------------------------------------------------
-- Business-date conversion
--
-- The database runs in UTC; the business runs in IST (UTC+05:30). A booking
-- created 02:00 IST is 20:30 UTC the PREVIOUS day, so casting a UTC timestamp
-- to date misassigns every late-night event and silently shifts daily revenue.
--
-- Every reporting date in this project goes through this function. It is
-- IMMUTABLE (timestamptz AT TIME ZONE <literal> is immutable in PostgreSQL,
-- unlike the timestamp -> timestamptz direction which depends on the session
-- TimeZone), so it can be indexed.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meta.business_date(ts timestamptz)
RETURNS date
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT (ts AT TIME ZONE 'Asia/Kolkata')::date
$$;

COMMENT ON FUNCTION meta.business_date(timestamptz) IS
    'UTC instant -> IST calendar date. The single definition of "which day did this happen".';


-- ---------------------------------------------------------------------------
-- Pipeline run log — observability. Failures must be visible, not swallowed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.pipeline_run (
    run_id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pipeline        text        NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    status          text        NOT NULL DEFAULT 'running'
                                CHECK (status IN ('running','success','failed','partial')),
    rows_in         bigint,
    rows_out        bigint,
    rows_rejected   bigint,
    git_sha         text,
    notes           text,
    error_message   text
);

CREATE INDEX IF NOT EXISTS ix_pipeline_run_pipeline_started
    ON meta.pipeline_run (pipeline, started_at DESC);


-- ---------------------------------------------------------------------------
-- Data quality: rules are declared once, results accumulate over time so the
-- quality score can be trended rather than only observed at this instant.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.dq_rule (
    rule_id       text        PRIMARY KEY,
    dimension     text        NOT NULL
                              CHECK (dimension IN ('completeness','uniqueness','validity',
                                                   'consistency','timeliness','accuracy')),
    target_table  text        NOT NULL,
    target_column text,
    description   text        NOT NULL,
    severity      text        NOT NULL DEFAULT 'error'
                              CHECK (severity IN ('error','warning','info')),
    threshold_pct numeric(6,3) NOT NULL DEFAULT 0,
    is_active     boolean     NOT NULL DEFAULT true
);

COMMENT ON COLUMN meta.dq_rule.dimension IS
    'DAMA data-quality dimension. Grouping rules this way lets the health score be decomposed.';
COMMENT ON COLUMN meta.dq_rule.threshold_pct IS
    'Failure rate tolerated before the rule is considered breached. 0 = zero tolerance.';

CREATE TABLE IF NOT EXISTS meta.dq_result (
    result_id     bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_id       text        NOT NULL REFERENCES meta.dq_rule(rule_id),
    run_id        bigint      REFERENCES meta.pipeline_run(run_id),
    checked_at    timestamptz NOT NULL DEFAULT now(),
    rows_checked  bigint      NOT NULL,
    rows_failed   bigint      NOT NULL,
    failure_pct   numeric(7,4) GENERATED ALWAYS AS (
                      CASE WHEN rows_checked = 0 THEN 0
                           ELSE 100.0 * rows_failed / rows_checked END
                  ) STORED,
    passed        boolean     NOT NULL,
    sample_keys   jsonb,
    notes         text
);

CREATE INDEX IF NOT EXISTS ix_dq_result_rule_checked
    ON meta.dq_result (rule_id, checked_at DESC);

COMMENT ON COLUMN meta.dq_result.sample_keys IS
    'A few offending keys, so a failure is investigable rather than merely countable.';


-- ---------------------------------------------------------------------------
-- Metric dictionary — the executable answer to "one number means one thing".
--
-- This is not documentation about the metrics; it IS the metric registry, and
-- the SQL expression stored here is the one the semantic layer executes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.metric_definition (
    metric_key        text        PRIMARY KEY,
    display_name      text        NOT NULL,
    business_definition text      NOT NULL,
    formula_text      text        NOT NULL,
    sql_expression    text        NOT NULL,
    powerbi_expression text,
    grain             text        NOT NULL,
    date_basis        text        NOT NULL
                                  CHECK (date_basis IN ('stay_date','booking_date','cancel_date',
                                                        'payment_date','request_date','resolved_date',
                                                        'review_date','not_applicable')),
    unit              text        NOT NULL
                                  CHECK (unit IN ('inr','percent','count','nights','days',
                                                  'hours','minutes','ratio','score')),
    revenue_basis     text        CHECK (revenue_basis IN ('gross_incl_tax','net_of_tax',
                                                           'net_of_tax_and_commission','not_applicable')),
    includes_comp_units    boolean,
    includes_ooo_in_denom  boolean,
    includes_microstays    boolean,
    source_tables     text[]      NOT NULL,
    inclusion_rules   text,
    exclusion_rules   text,
    caveats           text,
    owner_team        text        NOT NULL,
    effective_from    date        NOT NULL DEFAULT DATE '2025-01-01',
    is_active         boolean     NOT NULL DEFAULT true
);

COMMENT ON TABLE meta.metric_definition IS
    'Executable metric registry. date_basis is CHECK-constrained so a metric cannot be '
    'published without declaring which date it is measured on - the single most common '
    'cause of two dashboards disagreeing.';


-- ---------------------------------------------------------------------------
-- Lineage — machine-readable source-to-metric edges, so the lineage diagram is
-- generated from the system rather than hand-drawn and drifting.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.lineage_edge (
    edge_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_layer   text NOT NULL CHECK (source_layer IN ('source_system','raw','staging','mart','metric')),
    source_object  text NOT NULL,
    target_layer   text NOT NULL CHECK (target_layer IN ('raw','staging','mart','metric','dashboard')),
    target_object  text NOT NULL,
    transform_note text,
    refresh_cadence text,
    UNIQUE (source_layer, source_object, target_layer, target_object)
);


-- ---------------------------------------------------------------------------
-- LLM run log — measured token cost, never estimated.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.llm_run_log (
    llm_run_id     bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_at         timestamptz NOT NULL DEFAULT now(),
    feature        text        NOT NULL,
    model          text        NOT NULL,
    records_in     integer     NOT NULL,
    records_ok     integer     NOT NULL,
    records_quarantined integer NOT NULL DEFAULT 0,
    prompt_tokens  bigint,
    output_tokens  bigint,
    total_tokens   bigint,
    wall_clock_s   numeric(10,2),
    notes          text
);

COMMENT ON TABLE meta.llm_run_log IS
    'Actual usage_metadata from every LLM call. The cost figures in the README are '
    'measured from this table, not estimated from a price list.';
