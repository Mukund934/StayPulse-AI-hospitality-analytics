-- 003 · Fact tables.
--
-- The load-bearing decision in this file is that occupancy is NOT computed from
-- fact_booking. Bookings span nights; occupancy is a per-night question. Deriving
-- room-nights inside every query is how the departure night gets counted, which
-- inflates room-nights by roughly 1/ALOS (~33% at a 3-night stay) and is the most
-- common defect in hospitality analytics. fact_unit_night materialises the
-- half-open interval [check_in, check_out) exactly once, in one place.
--
-- Idempotent.

-- ---------------------------------------------------------------------------
-- Booking — one row per reservation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.fact_booking (
    booking_key      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    booking_id       text    NOT NULL UNIQUE,
    guest_key        integer NOT NULL REFERENCES mart.dim_guest(guest_key),
    property_key     integer NOT NULL REFERENCES mart.dim_property(property_key),
    unit_key         integer          REFERENCES mart.dim_unit(unit_key),
    channel_key      integer NOT NULL REFERENCES mart.dim_channel(channel_key),

    -- Four distinct dates. Each answers a different question and they are NOT
    -- interchangeable: Marketing reports on booked_at, Operations on stay dates,
    -- Accounts on payment date. Every metric declares which one it uses.
    booked_at        timestamptz NOT NULL,
    booking_date     date    NOT NULL,     -- meta.business_date(booked_at)
    check_in_date    date    NOT NULL,
    check_out_date   date    NOT NULL,
    cancelled_at     timestamptz,
    cancel_date      date,

    stay_type        text    NOT NULL DEFAULT 'nightly'
                             CHECK (stay_type IN ('nightly','microstay','day_use')),
    nights           smallint NOT NULL,
    adults           smallint NOT NULL DEFAULT 1,
    status           text    NOT NULL
                             CHECK (status IN ('confirmed','checked_in','checked_out',
                                               'cancelled','no_show')),

    -- Money. Stored pre-tax; tax is derived from meta.gst_rate by stay date so
    -- the rate change on 22 Sep 2025 is applied correctly rather than baked in.
    gross_amount_inr    numeric(12,2) NOT NULL CHECK (gross_amount_inr >= 0),
    discount_inr        numeric(12,2) NOT NULL DEFAULT 0 CHECK (discount_inr >= 0),
    net_room_amount_inr numeric(12,2) NOT NULL CHECK (net_room_amount_inr >= 0),
    gst_amount_inr      numeric(12,2) NOT NULL DEFAULT 0,
    commission_inr      numeric(12,2) NOT NULL DEFAULT 0,
    lead_time_days      smallint,

    source_system    text NOT NULL DEFAULT 'pms',
    ingested_at      timestamptz NOT NULL DEFAULT now(),

    -- Data-quality guards. These are deliberately NOT enforced as constraints on
    -- the raw/staging layers: defects must survive far enough to be measured.
    CONSTRAINT ck_booking_dates CHECK (check_out_date >= check_in_date),
    CONSTRAINT ck_booking_cancel CHECK (
        (status = 'cancelled' AND cancelled_at IS NOT NULL)
        OR (status <> 'cancelled' AND cancelled_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_booking_stay     ON mart.fact_booking (check_in_date, check_out_date);
CREATE INDEX IF NOT EXISTS ix_booking_bdate    ON mart.fact_booking (booking_date);
CREATE INDEX IF NOT EXISTS ix_booking_property ON mart.fact_booking (property_key, check_in_date);
CREATE INDEX IF NOT EXISTS ix_booking_channel  ON mart.fact_booking (channel_key);
CREATE INDEX IF NOT EXISTS ix_booking_guest    ON mart.fact_booking (guest_key);

COMMENT ON COLUMN mart.fact_booking.nights IS
    'check_out_date - check_in_date. Zero for microstays and day-use. The half-open '
    'interval means the departure night is not a night.';
COMMENT ON COLUMN mart.fact_booking.stay_type IS
    'Hourly microstays are sold through a separate channel and can occupy the same '
    'unit twice in one day. Counting one as a nightly stay destroys ADR, so ADR is '
    'reported both including and excluding them.';


-- ---------------------------------------------------------------------------
-- Unit-night — one row per sellable unit per calendar night, occupied or not.
--
-- Serves BOTH sides of every rate metric:
--   rooms available = count(*) where is_sellable
--   rooms sold      = count(*) where is_occupied
--   RevPAR          = sum(room_revenue_net) / count(*) where is_sellable
-- so occupancy and ADR can never be computed off different denominators.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.fact_unit_night (
    unit_night_key   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    unit_key         integer NOT NULL REFERENCES mart.dim_unit(unit_key),
    property_key     integer NOT NULL REFERENCES mart.dim_property(property_key),
    stay_date        date    NOT NULL,
    date_key         integer NOT NULL REFERENCES mart.dim_date(date_key),

    booking_key      bigint  REFERENCES mart.fact_booking(booking_key),
    channel_key      integer REFERENCES mart.dim_channel(channel_key),

    is_sellable      boolean NOT NULL DEFAULT true,
    is_occupied      boolean NOT NULL DEFAULT false,
    is_out_of_order  boolean NOT NULL DEFAULT false,
    is_complimentary boolean NOT NULL DEFAULT false,

    room_revenue_net_inr numeric(12,2) NOT NULL DEFAULT 0,
    gst_inr              numeric(12,2) NOT NULL DEFAULT 0,
    commission_inr       numeric(12,2) NOT NULL DEFAULT 0,

    UNIQUE (unit_key, stay_date),
    CONSTRAINT ck_occupied_has_booking CHECK (NOT is_occupied OR booking_key IS NOT NULL),
    CONSTRAINT ck_ooo_not_sellable     CHECK (NOT is_out_of_order OR NOT is_sellable)
);

CREATE INDEX IF NOT EXISTS ix_unit_night_date     ON mart.fact_unit_night (stay_date);
CREATE INDEX IF NOT EXISTS ix_unit_night_prop_date ON mart.fact_unit_night (property_key, stay_date);
CREATE INDEX IF NOT EXISTS ix_unit_night_booking  ON mart.fact_unit_night (booking_key);

COMMENT ON TABLE mart.fact_unit_night IS
    'One row per unit per night, occupied or not. The denominator and the numerator '
    'of every rate metric come from the same table, so RevPAR = ADR x Occupancy is '
    'an identity that can be asserted in a test rather than hoped for.';
COMMENT ON COLUMN mart.fact_unit_night.is_out_of_order IS
    'Out of order removes the unit from availability. Occupancy is reported both '
    'ways - excluding OOO (operational view) and including it (benchmark view) - '
    'because the two differ and the gap is itself an actionable number.';


-- ---------------------------------------------------------------------------
-- Payment / settlement — supports three-way reconciliation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.fact_payment (
    payment_key      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id       text    NOT NULL UNIQUE,
    booking_key      bigint  REFERENCES mart.fact_booking(booking_key),
    booking_id_raw   text,
    paid_at          timestamptz NOT NULL,
    payment_date     date    NOT NULL,
    settled_at       timestamptz,
    settlement_date  date,
    method           text    NOT NULL CHECK (method IN ('upi','card','netbanking','wallet','cash','ota_collect')),
    gross_amount_inr numeric(12,2) NOT NULL,
    gateway_fee_inr  numeric(12,2) NOT NULL DEFAULT 0,
    gst_on_fee_inr   numeric(12,2) NOT NULL DEFAULT 0,
    net_credited_inr numeric(12,2),
    is_refund        boolean NOT NULL DEFAULT false,
    status           text    NOT NULL CHECK (status IN ('captured','settled','failed','refunded','pending')),
    source_system    text    NOT NULL DEFAULT 'gateway'
);

CREATE INDEX IF NOT EXISTS ix_payment_booking ON mart.fact_payment (booking_key);
CREATE INDEX IF NOT EXISTS ix_payment_date    ON mart.fact_payment (payment_date);

COMMENT ON COLUMN mart.fact_payment.booking_id_raw IS
    'The booking reference exactly as the gateway supplied it. Retained even when '
    'it fails to resolve, because an unresolvable reference is the finding.';
COMMENT ON COLUMN mart.fact_payment.net_credited_inr IS
    'What the bank actually credited. gross - fee - GST on fee. A reconciliation is '
    'clean when UNEXPLAINED variance is zero, not when variance is zero.';


-- ---------------------------------------------------------------------------
-- Service request
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.fact_service_request (
    request_key       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id        text    NOT NULL UNIQUE,
    property_key      integer NOT NULL REFERENCES mart.dim_property(property_key),
    unit_key          integer REFERENCES mart.dim_unit(unit_key),
    booking_key       bigint  REFERENCES mart.fact_booking(booking_key),
    guest_key         integer REFERENCES mart.dim_guest(guest_key),
    request_type_key  integer NOT NULL REFERENCES mart.dim_request_type(request_type_key),
    assigned_staff_key integer REFERENCES mart.dim_staff(staff_key),

    created_at        timestamptz NOT NULL,
    request_date      date    NOT NULL,
    first_response_at timestamptz,
    resolved_at       timestamptz,
    resolved_date     date,

    priority          text    NOT NULL CHECK (priority IN ('P1','P2','P3')),
    status            text    NOT NULL CHECK (status IN ('open','in_progress','resolved','closed','reopened')),
    channel           text    CHECK (channel IN ('whatsapp','phone','front_desk','app','email')),
    sla_minutes       integer NOT NULL,
    resolution_minutes integer,
    is_sla_breached   boolean,
    reopened_count    smallint NOT NULL DEFAULT 0,
    csat_score        smallint CHECK (csat_score BETWEEN 1 AND 5),

    CONSTRAINT ck_request_resolution CHECK (resolved_at IS NULL OR resolved_at >= created_at)
);

CREATE INDEX IF NOT EXISTS ix_request_property_date ON mart.fact_service_request (property_key, request_date);
CREATE INDEX IF NOT EXISTS ix_request_type          ON mart.fact_service_request (request_type_key);
CREATE INDEX IF NOT EXISTS ix_request_breach        ON mart.fact_service_request (is_sla_breached) WHERE is_sla_breached;

COMMENT ON COLUMN mart.fact_service_request.resolution_minutes IS
    'Wall-clock minutes from creation to resolution. Wall-clock, not business hours: '
    'these are 24-hour serviced apartments, and a guest at 02:00 is still waiting.';


-- ---------------------------------------------------------------------------
-- Review (structured) and its AI-extracted aspects (kept separate on purpose)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.fact_review (
    review_key     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_id      text    NOT NULL UNIQUE,
    property_key   integer NOT NULL REFERENCES mart.dim_property(property_key),
    booking_key    bigint  REFERENCES mart.fact_booking(booking_key),
    guest_key      integer REFERENCES mart.dim_guest(guest_key),
    channel_key    integer REFERENCES mart.dim_channel(channel_key),
    reviewed_at    timestamptz NOT NULL,
    review_date    date    NOT NULL,
    rating         numeric(2,1) CHECK (rating BETWEEN 1 AND 5),
    review_text    text,
    language       text    CHECK (language IN ('en','hi','hinglish','other')),
    is_synthetic_text boolean NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS ix_review_property_date ON mart.fact_review (property_key, review_date);

COMMENT ON COLUMN mart.fact_review.is_synthetic_text IS
    'Distinguishes generated demo text from real public-corpus text used for AI '
    'evaluation. Measuring a model against text a model wrote proves little; the '
    'reported accuracy must come from real human writing.';

-- Aspect grain: a review praising the apartment and damning check-in becomes two
-- rows, not one score. Document-level sentiment cannot be routed to a team.
CREATE TABLE IF NOT EXISTS mart.fact_review_aspect (
    aspect_key      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_key      bigint  NOT NULL REFERENCES mart.fact_review(review_key) ON DELETE CASCADE,
    property_key    integer NOT NULL REFERENCES mart.dim_property(property_key),
    review_date     date    NOT NULL,
    category        text    NOT NULL,
    polarity        text    NOT NULL CHECK (polarity IN ('positive','negative','neutral')),
    severity        text    NOT NULL CHECK (severity IN ('minor','moderate','severe','not_applicable')),
    confidence      text    NOT NULL CHECK (confidence IN ('high','medium','low')),
    actionable_by   text    CHECK (actionable_by IN ('housekeeping','front_office','maintenance',
                                                     'revenue','tech','none')),
    evidence_span   text    NOT NULL,
    evidence_verified boolean NOT NULL DEFAULT false,
    model           text    NOT NULL,
    extracted_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_aspect_review   ON mart.fact_review_aspect (review_key);
CREATE INDEX IF NOT EXISTS ix_aspect_cat_date ON mart.fact_review_aspect (category, review_date);

COMMENT ON COLUMN mart.fact_review_aspect.evidence_span IS
    'Verbatim quote from the source review. A post-processing assertion checks it is '
    'a literal substring of review_text; anything failing is quarantined, not published.';
COMMENT ON COLUMN mart.fact_review_aspect.evidence_verified IS
    'Result of that substring assertion. Only verified rows reach a dashboard.';


-- ---------------------------------------------------------------------------
-- Consumables inventory
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mart.fact_inventory_movement (
    movement_key   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    property_key   integer NOT NULL REFERENCES mart.dim_property(property_key),
    movement_date  date    NOT NULL,
    item_code      text    NOT NULL,
    item_name      text    NOT NULL,
    opening_qty    integer NOT NULL,
    received_qty   integer NOT NULL DEFAULT 0,
    consumed_qty   integer NOT NULL DEFAULT 0,
    wastage_qty    integer NOT NULL DEFAULT 0,
    closing_qty    integer NOT NULL,
    unit_cost_inr  numeric(10,2),
    UNIQUE (property_key, movement_date, item_code)
);

CREATE INDEX IF NOT EXISTS ix_inventory_date ON mart.fact_inventory_movement (movement_date);
