// StayPulse - Power Query loader for the exported star schema.
//
// HOW TO USE
//   1. Power BI Desktop -> Home -> Transform data -> Advanced Editor
//   2. Paste this whole script over the contents of a new blank query.
//   3. Point DataFolder at your local powerbi/data path (keep the trailing slash).
//   4. Close & Apply, then reference each table from its own blank query:
//        = LoadAll[dim_channel]
//      or right-click the returned record -> Add as New Query.
//
// WHY A SCRIPT AND NOT THE FOLDER CONNECTOR
//   The folder connector combines files that share ONE shape. These are 13
//   DIFFERENT tables, so each needs its own explicit type contract. Types are
//   declared by hand rather than auto-detected because auto-detection is
//   locale-sensitive: 03/04 is 3 April or 4 March depending on the machine.
//
// Regenerate the CSVs any time with:  python scripts/export_bi_model.py

let
  // ---- CHANGE THIS ONE LINE ----
  DataFolder = "C:\\Users\\mukun\\Downloads\\VS_Code\\Github\\StayPulseAI\\powerbi\\data\\",
  // -- dim_channel (7 columns) --
  dim_channel =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "dim_channel.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"channel_key", Int64.Type}, {"channel_code", type text}, {"channel_name", type text}, {"channel_type", type text}, {"commission_pct", type number}, {"settlement_days", Int64.Type}, {"is_active", type logical}})
    in
      Typed,

  // -- dim_date (14 columns) --
  dim_date =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "dim_date.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"date_key", Int64.Type}, {"full_date", type date}, {"year", Int64.Type}, {"quarter", Int64.Type}, {"month", Int64.Type}, {"month_name", Int64.Type}, {"year_month", Int64.Type}, {"day_of_month", Int64.Type}, {"day_of_week", Int64.Type}, {"day_name", type text}, {"week_of_year", Int64.Type}, {"is_weekend", type logical}, {"is_business_night", type logical}, {"same_day_last_year", type date}})
    in
      Typed,

  // -- dim_guest (6 columns) --
  dim_guest =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "dim_guest.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"guest_key", Int64.Type}, {"guest_id", type text}, {"home_city", type text}, {"guest_segment", type text}, {"has_phone", type logical}, {"has_email", type logical}})
    in
      Typed,

  // -- dim_property (11 columns) --
  dim_property =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "dim_property.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"property_key", Int64.Type}, {"property_code", type text}, {"property_name", type text}, {"area", type text}, {"city", type text}, {"state", type text}, {"unit_count", Int64.Type}, {"opened_on", type date}, {"closed_on", type date}, {"is_active", type logical}, {"has_restaurant", type logical}})
    in
      Typed,

  // -- dim_request_type (6 columns) --
  dim_request_type =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "dim_request_type.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"request_type_key", Int64.Type}, {"category", type text}, {"subcategory", type text}, {"default_priority", type text}, {"sla_minutes", Int64.Type}, {"owning_team", type text}})
    in
      Typed,

  // -- dim_unit (10 columns) --
  dim_unit =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "dim_unit.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"unit_key", Int64.Type}, {"unit_code", type text}, {"property_key", Int64.Type}, {"unit_type", type text}, {"bedrooms", Int64.Type}, {"max_occupancy", Int64.Type}, {"sqft", Int64.Type}, {"floor", Int64.Type}, {"base_rate_inr", type number}, {"is_sellable", type logical}})
    in
      Typed,

  // -- dq_rule_results (10 columns) --
  dq_rule_results =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "dq_rule_results.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"rule_id", type text}, {"dimension", type text}, {"target_table", type text}, {"severity", type text}, {"description", type text}, {"rows_checked", Int64.Type}, {"rows_failed", Int64.Type}, {"failure_pct", type number}, {"passed", type logical}, {"checked_at", type datetimezone}})
    in
      Typed,

  // -- fact_booking (19 columns) --
  fact_booking =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "fact_booking.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"booking_key", Int64.Type}, {"booking_id", type text}, {"guest_key", Int64.Type}, {"property_key", Int64.Type}, {"channel_key", Int64.Type}, {"booking_date", type date}, {"check_in_date", type date}, {"check_out_date", type date}, {"cancel_date", type date}, {"stay_type", type text}, {"status", type text}, {"is_cancelled", type logical}, {"nights", Int64.Type}, {"lead_time_days", Int64.Type}, {"net_room_amount_inr", type number}, {"discount_inr", type number}, {"commission_inr", type number}, {"nightly_rate_inr", type number}, {"has_business_date_drift", type logical}})
    in
      Typed,

  // -- fact_daily_kpi (23 columns) --
  fact_daily_kpi =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "fact_daily_kpi.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"property_key", Int64.Type}, {"property_code", type text}, {"stay_date", type date}, {"date_key", Int64.Type}, {"year_month", Int64.Type}, {"day_name", type text}, {"is_weekend", type logical}, {"rooms_available", Int64.Type}, {"rooms_out_of_order", Int64.Type}, {"unit_nights_physical", Int64.Type}, {"rooms_sold", Int64.Type}, {"rooms_sold_microstay", Int64.Type}, {"room_revenue_net_inr", type number}, {"gst_inr", type number}, {"room_revenue_gross_inr", type number}, {"commission_inr", type number}, {"net_after_commission_inr", type number}, {"occupancy_pct", type number}, {"occupancy_pct_benchmark", type number}, {"occupancy_ooo_gap_pp", type number}, {"adr_inr", type number}, {"adr_excl_microstay_inr", type number}, {"revpar_inr", type number}})
    in
      Typed,

  // -- fact_payment (12 columns) --
  fact_payment =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "fact_payment.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"payment_key", Int64.Type}, {"payment_id", type text}, {"booking_key", Int64.Type}, {"payment_date", type date}, {"settlement_date", type date}, {"method", type text}, {"gross_amount_inr", type number}, {"gateway_fee_inr", type number}, {"gst_on_fee_inr", type date}, {"net_credited_inr", type number}, {"status", type text}, {"is_orphan_reference", type logical}})
    in
      Typed,

  // -- fact_review_aspect (13 columns) --
  fact_review_aspect =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "fact_review_aspect.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"aspect_key", Int64.Type}, {"review_key", Int64.Type}, {"property_key", Int64.Type}, {"review_date", type date}, {"category", type text}, {"polarity", type text}, {"severity", type text}, {"confidence", type text}, {"actionable_by", type text}, {"evidence_span", type text}, {"model", type text}, {"rating", type number}, {"language", type text}})
    in
      Typed,

  // -- fact_service_request (20 columns) --
  fact_service_request =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "fact_service_request.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"request_key", Int64.Type}, {"request_id", type text}, {"property_key", Int64.Type}, {"request_type_key", Int64.Type}, {"category", type text}, {"subcategory", type text}, {"owning_team", type text}, {"request_date", type date}, {"resolved_date", type date}, {"created_hour_ist", type text}, {"day_part_ist", type text}, {"priority", type text}, {"status", type text}, {"channel", type text}, {"sla_minutes", Int64.Type}, {"resolution_minutes", Int64.Type}, {"first_response_minutes", Int64.Type}, {"is_sla_breached", type logical}, {"reopened_count", Int64.Type}, {"csat_score", Int64.Type}})
    in
      Typed,

  // -- metric_definition (10 columns) --
  metric_definition =
    let
      Src     = Csv.Document(
                  File.Contents(DataFolder & "metric_definition.csv"),
                  [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),
      Typed   = Table.TransformColumnTypes(Headers, {{"metric_key", Int64.Type}, {"display_name", type text}, {"business_definition", type text}, {"formula_text", type text}, {"grain", type text}, {"date_basis", type date}, {"unit", type text}, {"revenue_basis", type text}, {"owner_team", type text}, {"caveats", type text}})
    in
      Typed

  LoadAll = [
    dim_channel = dim_channel,
    dim_date = dim_date,
    dim_guest = dim_guest,
    dim_property = dim_property,
    dim_request_type = dim_request_type,
    dim_unit = dim_unit,
    dq_rule_results = dq_rule_results,
    fact_booking = fact_booking,
    fact_daily_kpi = fact_daily_kpi,
    fact_payment = fact_payment,
    fact_review_aspect = fact_review_aspect,
    fact_service_request = fact_service_request,
    metric_definition = metric_definition
  ]
in
  LoadAll
