-- 007 · Calendar intelligence: public holidays and derived date context.
--
-- WHY THIS EXISTS
--
-- `dim_date` knew about weekdays and weekends and nothing else, so every model in
-- the warehouse was blind to the single largest non-weekly demand effect in the
-- dataset. Audit finding F-4.
--
--
-- WHY THERE IS NO HOLIDAY API BEHIND THIS
--
-- Nager.Date is the obvious free, zero-auth choice and it does NOT cover India.
-- Verified 2026-08-15: PublicHolidays/2025/IN returns HTTP 204 with an empty body
-- (same for 2024 and 2026), and 'IN' is absent from AvailableCountries, which
-- lists 204 countries. A US control call returns 200 with data, so the service is
-- healthy and India is simply missing.
--
-- The replacement is a committed, source-cited table in
-- data/reference/india_holidays.json, loaded by scripts/load_calendar.py. That is
-- not a workaround, it is the better fit: this dataset is frozen at 2026-08-11 and
-- will never need next year's holidays, so a live API would be serving a
-- requirement that does not exist -- while adding a key, a rate limit, a network
-- dependency in CI and a vendor outage mode.
--
--
-- WHAT THIS FILE DELIBERATELY DOES NOT STORE
--
-- No demand windows, and no effect multipliers.
--
-- The generator plants four suppressive festival windows (Diwali x0.62, year end
-- x0.70, Holi x0.80). If this migration encoded those windows, then measuring a
-- holiday effect "against the planted windows" would be circular -- the answer
-- would be assumed by the schema.
--
-- So the warehouse stores only what is externally true: the dates public holidays
-- fell on. The SHAPE and SIZE of any demand effect is measured from the data by
-- `staypulse.signals.calendar`, using an offset profile around each holiday that
-- assumes no window at all. The planted windows are read separately, from the
-- generator spec, and used ONLY as ground truth at validation time.
--
-- Idempotent.

-- ---------------------------------------------------------------------------
-- Provenance. A calendar nobody can audit is a calendar nobody should trust.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.calendar_source (
    source_key      text PRIMARY KEY,
    description     text        NOT NULL,
    origin          text        NOT NULL,
    coverage_from   date        NOT NULL,
    coverage_to     date        NOT NULL,
    entry_count     integer     NOT NULL,
    needs_review    integer     NOT NULL DEFAULT 0,
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    checksum_sha256 text
);

COMMENT ON TABLE meta.calendar_source IS
    'Provenance for the holiday calendar: where it came from, what it covers, and '
    'how many entries carry a date that must be confirmed by a human because it '
    'is set by a lunar calendar rather than by statute.';

COMMENT ON COLUMN meta.calendar_source.needs_review IS
    'Count of entries with confidence=lunar. These move year to year and are the '
    'only ones a human has to check.';


-- ---------------------------------------------------------------------------
-- Calendar context on the date dimension.
--
-- `is_holiday_adjacent` is separate from `is_public_holiday` on purpose. A
-- corporate guest does not cancel only on the holiday itself -- the trip that
-- would have straddled it disappears too, so demand sags either side. Collapsing
-- the two would hide exactly the effect this layer exists to measure.
-- ---------------------------------------------------------------------------
ALTER TABLE mart.dim_date ADD COLUMN IF NOT EXISTS is_public_holiday   boolean NOT NULL DEFAULT false;
ALTER TABLE mart.dim_date ADD COLUMN IF NOT EXISTS holiday_name        text;
ALTER TABLE mart.dim_date ADD COLUMN IF NOT EXISTS holiday_scope       text;
ALTER TABLE mart.dim_date ADD COLUMN IF NOT EXISTS holiday_confidence  text;
ALTER TABLE mart.dim_date ADD COLUMN IF NOT EXISTS days_to_holiday     integer;
ALTER TABLE mart.dim_date ADD COLUMN IF NOT EXISTS nearest_holiday     text;
ALTER TABLE mart.dim_date ADD COLUMN IF NOT EXISTS is_holiday_adjacent boolean NOT NULL DEFAULT false;
ALTER TABLE mart.dim_date ADD COLUMN IF NOT EXISTS is_long_weekend     boolean NOT NULL DEFAULT false;
ALTER TABLE mart.dim_date ADD COLUMN IF NOT EXISTS is_bridge_day       boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN mart.dim_date.days_to_holiday IS
    'Signed offset to the nearest public holiday: negative before, 0 on the day, '
    'positive after. This is the axis the effect profile is measured along.';

COMMENT ON COLUMN mart.dim_date.is_holiday_adjacent IS
    'Within the adjacency radius of a public holiday. Set by the loader, not '
    'hardcoded here, so the radius is a documented parameter rather than schema.';

COMMENT ON COLUMN mart.dim_date.is_bridge_day IS
    'A single working day trapped between a public holiday and a weekend. In a '
    'corporate market these behave like holidays because people take them off.';

COMMENT ON COLUMN mart.dim_date.holiday_confidence IS
    'fixed = statutory date, cannot move. lunar = set by the Hindu or Islamic '
    'calendar and confirmed by a human against an official source.';

DROP INDEX IF EXISTS mart.ix_dim_date_holiday;
CREATE INDEX ix_dim_date_holiday ON mart.dim_date (is_public_holiday, full_date);

DROP INDEX IF EXISTS mart.ix_dim_date_offset;
CREATE INDEX ix_dim_date_offset ON mart.dim_date (days_to_holiday);


-- ---------------------------------------------------------------------------
-- Daily KPIs with calendar context attached, so the effect can be measured
-- without every consumer re-deriving the join.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_daily_kpi_calendar AS
SELECT k.*,
       d.is_public_holiday,
       d.holiday_name,
       d.holiday_scope,
       d.days_to_holiday,
       d.nearest_holiday,
       d.is_holiday_adjacent,
       d.is_long_weekend,
       d.is_bridge_day
FROM mart.v_daily_kpi k
JOIN mart.dim_date d ON d.date_key = k.date_key;

COMMENT ON VIEW mart.v_daily_kpi_calendar IS
    'Daily KPIs joined to calendar context. The measurement surface for holiday '
    'effects; carries no effect estimate itself.';


-- ---------------------------------------------------------------------------
-- Register the calendar metrics.
-- ---------------------------------------------------------------------------
INSERT INTO meta.metric_definition (
    metric_key, display_name, business_definition, formula_text, sql_expression,
    powerbi_expression, grain, date_basis, unit, revenue_basis,
    includes_comp_units, includes_ooo_in_denom, includes_microstays,
    source_tables, inclusion_rules, exclusion_rules, caveats, owner_team
) VALUES

('holiday_occupancy_effect_pp', 'Holiday occupancy effect',
 'Difference in occupancy between dates at a given offset from a public holiday and comparable non-holiday dates, in percentage points.',
 'Occupancy(offset) - Occupancy(comparable baseline)',
 'avg(occupancy_pct) FILTER (WHERE days_to_holiday = :offset) - avg(occupancy_pct) FILTER (WHERE NOT is_holiday_adjacent)',
 'CALCULATE([Occupancy], ''Date''[days_to_holiday] = 0) - CALCULATE([Occupancy], NOT ''Date''[is_holiday_adjacent])',
 'offset x property', 'stay_date', 'percent', 'not_applicable',
 true, false, true,
 ARRAY['mart.v_daily_kpi_calendar'],
 'Baseline is same-weekday dates outside the adjacency radius, so the weekly cycle cannot leak into the estimate.',
 'Dates within the adjacency radius of a DIFFERENT holiday are excluded from the baseline.',
 'On this portfolio the effect is NEGATIVE: it is a corporate aparthotel and business travel stops during festivals, the inverse of a leisure property. Sample is small - three festival windows fall inside the data - so the interval is wide and is reported alongside the point estimate.',
 'Revenue'),

('is_holiday_adjacent', 'Holiday adjacency',
 'Whether a stay date falls within the adjacency radius of a public holiday.',
 'abs(days_to_holiday) <= adjacency_radius',
 'd.is_holiday_adjacent',
 '''Date''[is_holiday_adjacent]',
 'stay_date', 'stay_date', 'count', 'not_applicable',
 false, false, false,
 ARRAY['mart.dim_date'],
 'Radius is a documented loader parameter, not a schema constant.',
 'Holidays outside the dataset window are still recorded but cannot be measured.',
 'Adjacency is a flag, not an effect. The effect is measured, never assumed.',
 'Revenue')

ON CONFLICT (metric_key) DO UPDATE SET
    display_name        = EXCLUDED.display_name,
    business_definition = EXCLUDED.business_definition,
    formula_text        = EXCLUDED.formula_text,
    sql_expression      = EXCLUDED.sql_expression,
    powerbi_expression  = EXCLUDED.powerbi_expression,
    grain               = EXCLUDED.grain,
    date_basis          = EXCLUDED.date_basis,
    unit                = EXCLUDED.unit,
    source_tables       = EXCLUDED.source_tables,
    inclusion_rules     = EXCLUDED.inclusion_rules,
    exclusion_rules     = EXCLUDED.exclusion_rules,
    caveats             = EXCLUDED.caveats;
