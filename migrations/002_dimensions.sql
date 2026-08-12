-- 002 · Conformed dimensions.
--
-- Surrogate integer keys for joins, natural business keys retained and uniquely
-- constrained so a re-load cannot silently duplicate a dimension member.
-- Idempotent.

-- ---------------------------------------------------------------------------
-- Date
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.dim_date (
    date_key        integer PRIMARY KEY,          -- yyyymmdd
    full_date       date    NOT NULL UNIQUE,
    year            smallint NOT NULL,
    quarter         smallint NOT NULL,
    month           smallint NOT NULL,
    month_name      text     NOT NULL,
    year_month      text     NOT NULL,            -- 2026-08
    day_of_month    smallint NOT NULL,
    day_of_week     smallint NOT NULL,            -- ISO: 1=Mon .. 7=Sun
    day_name        text     NOT NULL,
    week_of_year    smallint NOT NULL,
    is_weekend      boolean  NOT NULL,
    -- Bengaluru corporate demand is weekday-heavy, so "business night" is the
    -- more useful flag than the calendar weekend.
    is_business_night boolean NOT NULL,
    -- 364 = exactly 52 weeks, so a Saturday maps to a Saturday. Weekday
    -- alignment matters more than calendar date in hospitality comparison.
    same_day_last_year date  NOT NULL
);

COMMENT ON COLUMN mart.dim_date.same_day_last_year IS
    'full_date - 364 days. 364 preserves weekday alignment; 365 does not.';

INSERT INTO mart.dim_date (
    date_key, full_date, year, quarter, month, month_name, year_month,
    day_of_month, day_of_week, day_name, week_of_year, is_weekend,
    is_business_night, same_day_last_year
)
SELECT
    to_char(d, 'YYYYMMDD')::integer,
    d::date,
    extract(year    FROM d)::smallint,
    extract(quarter FROM d)::smallint,
    extract(month   FROM d)::smallint,
    trim(to_char(d, 'Month')),
    to_char(d, 'YYYY-MM'),
    extract(day FROM d)::smallint,
    extract(isodow FROM d)::smallint,
    trim(to_char(d, 'Day')),
    extract(week FROM d)::smallint,
    extract(isodow FROM d) IN (6, 7),
    extract(isodow FROM d) BETWEEN 1 AND 4,   -- Mon-Thu nights: corporate stay pattern
    (d - INTERVAL '364 days')::date
FROM generate_series(DATE '2024-01-01', DATE '2027-12-31', INTERVAL '1 day') AS d
ON CONFLICT (date_key) DO NOTHING;


-- ---------------------------------------------------------------------------
-- Property
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.dim_property (
    property_key   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_code  text    NOT NULL UNIQUE,
    property_name  text    NOT NULL,
    area           text    NOT NULL,
    city           text    NOT NULL DEFAULT 'Bengaluru',
    state          text    NOT NULL DEFAULT 'Karnataka',
    unit_count     smallint NOT NULL CHECK (unit_count > 0),
    opened_on      date    NOT NULL,
    closed_on      date,
    is_active      boolean NOT NULL DEFAULT true,
    has_restaurant boolean NOT NULL DEFAULT false,
    CHECK (closed_on IS NULL OR closed_on > opened_on)
);

COMMENT ON TABLE mart.dim_property IS
    'Serviced aparthotels. Properties are in Bengaluru; operations are run from a '
    'back office ~1,200 km away, which is why consolidated reporting exists at all.';
COMMENT ON COLUMN mart.dim_property.has_restaurant IS
    'False for every property. Retained explicitly so F&B metrics are provably '
    'out of scope rather than merely absent.';


-- ---------------------------------------------------------------------------
-- Unit (the sellable inventory item; "room" would be the wrong word here)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.dim_unit (
    unit_key       integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    unit_code      text    NOT NULL UNIQUE,
    property_key   integer NOT NULL REFERENCES mart.dim_property(property_key),
    unit_type      text    NOT NULL CHECK (unit_type IN ('Studio','1BHK','1BHK Large','2BHK','2BHK Premium')),
    bedrooms       smallint NOT NULL CHECK (bedrooms BETWEEN 0 AND 3),
    max_occupancy  smallint NOT NULL CHECK (max_occupancy BETWEEN 1 AND 8),
    sqft           smallint,
    floor          smallint,
    base_rate_inr  numeric(10,2) NOT NULL CHECK (base_rate_inr > 0),
    is_sellable    boolean NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS ix_dim_unit_property ON mart.dim_unit (property_key);

COMMENT ON COLUMN mart.dim_unit.base_rate_inr IS
    'Pre-tax list rate. GST is applied on top and depends on whether this crosses '
    'the INR 7,500/night threshold - see meta.gst_rate.';


-- ---------------------------------------------------------------------------
-- Channel
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.dim_channel (
    channel_key      integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_code     text    NOT NULL UNIQUE,
    channel_name     text    NOT NULL,
    channel_type     text    NOT NULL CHECK (channel_type IN ('ota','direct','corporate','walk_in','hourly')),
    commission_pct   numeric(5,2) NOT NULL DEFAULT 0 CHECK (commission_pct BETWEEN 0 AND 40),
    settlement_days  smallint NOT NULL DEFAULT 0,
    is_active        boolean NOT NULL DEFAULT true
);

COMMENT ON COLUMN mart.dim_channel.commission_pct IS
    'Charged on the pre-tax room rate. GST is then charged on the commission itself, '
    'so gross-to-net is a two-step calculation, not one.';
COMMENT ON COLUMN mart.dim_channel.settlement_days IS
    'Days from checkout to bank credit. Drives the timing component of payment '
    'reconciliation variance.';


-- ---------------------------------------------------------------------------
-- Guest
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.dim_guest (
    guest_key        integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guest_id         text    NOT NULL UNIQUE,
    full_name        text    NOT NULL,
    email            text,
    phone            text,
    -- Deterministic identity keys. Populated by the ETL, not the source: the
    -- same person books as "Ravi Kumar"/"ravi kumar "/"+91 98…"/"098…" and the
    -- repeat-guest rate is wrong until these are normalised.
    email_normalised text,
    phone_last10     text,
    home_city        text,
    guest_segment    text    CHECK (guest_segment IN ('corporate','leisure','relocation','unknown')),
    first_seen_date  date,
    is_duplicate_of  integer REFERENCES mart.dim_guest(guest_key)
);

CREATE INDEX IF NOT EXISTS ix_dim_guest_email_norm ON mart.dim_guest (email_normalised);
CREATE INDEX IF NOT EXISTS ix_dim_guest_phone10    ON mart.dim_guest (phone_last10);

COMMENT ON COLUMN mart.dim_guest.is_duplicate_of IS
    'Self-reference to the surviving guest record. Deliberately does NOT delete the '
    'duplicate: repeat rate before and after resolution is itself a finding.';


-- ---------------------------------------------------------------------------
-- Staff
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.dim_staff (
    staff_key     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    staff_id      text    NOT NULL UNIQUE,
    full_name     text    NOT NULL,
    role          text    NOT NULL CHECK (role IN ('housekeeping','maintenance','front_office','ops_manager')),
    property_key  integer REFERENCES mart.dim_property(property_key),
    shift         text    CHECK (shift IN ('morning','evening','night')),
    joined_on     date    NOT NULL,
    left_on       date,
    is_active     boolean NOT NULL DEFAULT true
);


-- ---------------------------------------------------------------------------
-- Service request taxonomy, carrying the SLA target that defines a breach
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.dim_request_type (
    request_type_key integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category         text    NOT NULL,
    subcategory      text    NOT NULL,
    default_priority text    NOT NULL CHECK (default_priority IN ('P1','P2','P3')),
    sla_minutes      integer NOT NULL CHECK (sla_minutes > 0),
    owning_team      text    NOT NULL,
    UNIQUE (category, subcategory)
);

COMMENT ON COLUMN mart.dim_request_type.sla_minutes IS
    'Target resolution in wall-clock minutes from request creation. The SLA clock '
    'definition is stated in meta.metric_definition, not inferred per query.';


-- ---------------------------------------------------------------------------
-- GST rate, versioned by stay date.
--
-- Notification 15/2025-Central Tax (Rate), 17 Sep 2025, effective 22 Sep 2025:
-- the 12% slab was abolished. At or below INR 7,500/night: 5% without input tax
-- credit. Above: 18% with full ITC. Indian folios are GST-inclusive, so rate
-- metrics computed off invoice totals are 5-18% overstated unless de-grossed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.gst_rate (
    gst_rate_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    effective_from  date    NOT NULL,
    effective_to    date,
    threshold_inr   numeric(10,2) NOT NULL,
    rate_at_or_below numeric(5,2) NOT NULL,
    rate_above      numeric(5,2) NOT NULL,
    itc_at_or_below boolean NOT NULL,
    itc_above       boolean NOT NULL,
    authority       text    NOT NULL,
    UNIQUE (effective_from)
);

INSERT INTO meta.gst_rate
    (effective_from, effective_to, threshold_inr, rate_at_or_below, rate_above,
     itc_at_or_below, itc_above, authority)
VALUES
    (DATE '2024-01-01', DATE '2025-09-21', 7500.00, 12.00, 18.00, true,  true,
     'Pre-GST 2.0 regime: 12% with ITC at or below the threshold.'),
    (DATE '2025-09-22', NULL,              7500.00,  5.00, 18.00, false, true,
     'Notification 15/2025-Central Tax (Rate) dated 17 Sep 2025, effective 22 Sep 2025. '
     '12% slab abolished; 5% without ITC at or below INR 7,500, 18% with ITC above.')
ON CONFLICT (effective_from) DO NOTHING;

COMMENT ON TABLE meta.gst_rate IS
    'Stay-date-versioned GST. Spanning the 22 Sep 2025 change means an apparent '
    'jump in gross revenue is a tax artefact, not performance - the dashboard must '
    'be able to show net and prove it.';
