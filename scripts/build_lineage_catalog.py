"""Populate data lineage and emit the data catalog.

WHY THIS EXISTS

`meta.lineage_edge` was created in migration 001 and never populated. A lineage
table with zero rows is worse than no lineage table: the schema advertises a
capability the warehouse does not have, and anyone who trusted it would be trusting
an empty set. The audit caught it, and this closes it.

WHERE THE EDGES COME FROM, AND WHY THEY ARE NOT HAND-WRITTEN

Most of this is EXTRACTED, not declared, which is the difference between lineage
that stays true and lineage that rots:

  view -> view, view -> table   read from pg_depend / pg_rewrite. This is
                                PostgreSQL's own dependency graph, the same one it
                                uses to refuse a DROP. It cannot drift from reality
                                because it IS reality.

  metric -> source table        read from meta.metric_definition.source_tables,
                                which is already mandatory and CHECK-constrained.

Only two classes are declared by hand, because no database can see them:

  source_system -> mart         the generator and loader, which run outside the DB.
  mart -> dashboard             the API, Power BI and Excel consumers.

Those are marked with their origin so a reader can tell extracted fact from
authored claim.


THE CATALOG AND ITS PII RULE

PII classification is applied by rule, not by opinion, and the rule is published in
the output. It is deliberately conservative: a column is treated as personal data if
its name matches a known-identifier pattern, whether or not this synthetic dataset
happens to contain anything sensitive in it. Classifying real-looking columns as
safe because the values are fake is exactly the habit that leaks a production
extract later.

Run:
    python scripts/build_lineage_catalog.py
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text  # noqa: E402

from staypulse import db  # noqa: E402

REPORTS = PROJECT_ROOT / "reports"

# PII classification rules. Order matters: the first match wins.
# Each entry is (pattern, classification, handling note).
PII_RULES: list[tuple[str, str, str]] = [
    (r"(^|_)(email|e_mail)($|_)", "direct_identifier",
     "Contact identifier. Never leaves the warehouse; not exposed by any API route."),
    (r"(^|_)phone($|_)", "direct_identifier",
     "Contact identifier. Used only as a normalised duplicate-detection key."),
    (r"(^|_)(guest_name|full_name|first_name|last_name)($|_)", "direct_identifier",
     "Name. Not exposed by any API route and not present in any BI export."),
    (r"(^|_)(passport|aadhaar|pan|govt_id|id_number)($|_)", "sensitive_identifier",
     "Government identifier. Not collected by this warehouse."),
    (r"(^|_)review_text($|_)", "free_text_may_contain_pii",
     "Guest-authored free text. Can contain names or contact details incidentally; "
     "quoted verbatim only as short evidence spans, never bulk-exported."),
    (r"(^|_)(guest_key|guest_id)($|_)", "pseudonymous_key",
     "Surrogate key. Re-identifying requires dim_guest, which is not exposed."),
    (r"(^|_)(booking_id|booking_key)($|_)", "indirect_identifier",
     "Transaction reference. Not personal on its own; identifying in combination."),
    (r"(^|_)(staff_name|staff_key)($|_)", "indirect_identifier",
     "Employee reference. Aggregated before any exposure."),
]


def classify(column: str) -> tuple[str, str]:
    for pattern, level, note in PII_RULES:
        if re.search(pattern, column):
            return level, note
    return "non_personal", ""


# ---------------------------------------------------------------------------
def extracted_edges() -> list[dict]:
    """Real object dependencies, straight out of PostgreSQL's own catalog."""
    rows = db.fetch_all("""
        SELECT DISTINCT
               src_ns.nspname || '.' || src.relname AS source_object,
               tgt_ns.nspname || '.' || tgt.relname AS target_object,
               src.relkind AS src_kind,
               tgt.relkind AS tgt_kind
        FROM pg_depend d
        JOIN pg_rewrite rw    ON rw.oid = d.objid
        JOIN pg_class tgt     ON tgt.oid = rw.ev_class
        JOIN pg_namespace tgt_ns ON tgt_ns.oid = tgt.relnamespace
        JOIN pg_class src     ON src.oid = d.refobjid
        JOIN pg_namespace src_ns ON src_ns.oid = src.relnamespace
        WHERE d.classid = 'pg_rewrite'::regclass
          AND d.deptype = 'n'
          AND src.oid <> tgt.oid
          AND src_ns.nspname IN ('mart', 'meta')
          AND tgt_ns.nspname IN ('mart', 'meta')
        ORDER BY 1, 2
    """)
    return [
        {
            "source_layer": "mart",
            "source_object": r["source_object"],
            "target_layer": "mart",
            "target_object": r["target_object"],
            "transform_note": (
                f"extracted from pg_depend: "
                f"{'view' if r['tgt_kind'] == 'v' else 'relation'} "
                f"{r['target_object']} reads "
                f"{'view' if r['src_kind'] == 'v' else 'table'} {r['source_object']}"
            ),
            "refresh_cadence": "on read (view)",
        }
        for r in rows
    ]


def metric_edges() -> list[dict]:
    """metric -> source object, read from the registry's own source_tables."""
    rows = db.fetch_all("""
        SELECT metric_key, unnest(source_tables) AS source_object, date_basis
        FROM meta.metric_definition WHERE is_active
        ORDER BY 1, 2
    """)
    return [
        {
            "source_layer": "mart",
            "source_object": r["source_object"],
            "target_layer": "metric",
            "target_object": r["metric_key"],
            "transform_note": (
                f"extracted from meta.metric_definition.source_tables; "
                f"date basis {r['date_basis']}"
            ),
            "refresh_cadence": "on query",
        }
        for r in rows
    ]


def declared_edges() -> list[dict]:
    """Edges outside the database, which no catalog can extract.

    Marked 'declared' so a reader can separate extracted fact from authored claim.
    """
    ingest = [
        ("seeded_generator", t) for t in (
            "mart.dim_property", "mart.dim_unit", "mart.dim_channel",
            "mart.dim_guest", "mart.dim_staff", "mart.dim_request_type",
            "mart.dim_date", "mart.fact_booking", "mart.fact_unit_night",
            "mart.fact_payment", "mart.fact_service_request", "mart.fact_review",
            "mart.fact_inventory_movement",
        )
    ]
    edges = [
        {
            "source_layer": "source_system",
            "source_object": src,
            "target_layer": "mart",
            "target_object": tgt,
            "transform_note": (
                "declared: seeded synthetic generator loaded via COPY "
                "(src/staypulse/generate)"
            ),
            "refresh_cadence": "on regeneration",
        }
        for src, tgt in ingest
    ]

    consumers = [
        ("mart.v_daily_kpi", "api:/api/kpis/overview"),
        ("mart.v_daily_kpi", "api:/api/revenue/trends"),
        ("mart.v_booking_night", "api:/api/revenue-management/pace"),
        ("mart.v_pickup_daily", "api:/api/revenue-management/pickup"),
        ("mart.v_booking_curve", "api:/api/revenue-management/booking-curve"),
        ("mart.v_cancellation_funnel", "api:/api/revenue-management/wash"),
        ("mart.v_lead_time_profile", "api:/api/revenue-management/lead-time"),
        ("mart.v_unit_night_enriched", "api:/api/revenue-management/why"),
        ("mart.v_service_kpi", "api:/api/operations/overview"),
        ("mart.v_buried_complaints", "api:/api/guest-intelligence/issues"),
        ("mart.v_daily_kpi", "powerbi:Executive Overview"),
        ("mart.v_daily_kpi", "excel:daily_kpi.xlsx"),
    ]
    edges += [
        {
            "source_layer": "mart",
            "source_object": src,
            "target_layer": "dashboard",
            "target_object": tgt,
            "transform_note": "declared: consumer reads the semantic layer directly",
            "refresh_cadence": "on request" if tgt.startswith("api") else "on refresh",
        }
        for src, tgt in consumers
    ]
    return edges


def write_edges(edges: list[dict]) -> int:
    with db.connect() as conn:
        conn.execute(text("DELETE FROM meta.lineage_edge"))
        for e in edges:
            conn.execute(
                text("""
                    INSERT INTO meta.lineage_edge
                        (source_layer, source_object, target_layer, target_object,
                         transform_note, refresh_cadence)
                    VALUES (:sl, :so, :tl, :to_, :note, :cad)
                    ON CONFLICT (source_layer, source_object, target_layer, target_object)
                    DO UPDATE SET transform_note = EXCLUDED.transform_note,
                                  refresh_cadence = EXCLUDED.refresh_cadence
                """),
                {"sl": e["source_layer"], "so": e["source_object"],
                 "tl": e["target_layer"], "to_": e["target_object"],
                 "note": e["transform_note"], "cad": e["refresh_cadence"]},
            )
    return db.scalar("SELECT count(*) FROM meta.lineage_edge")


# ---------------------------------------------------------------------------
def build_catalog() -> dict:
    cols = db.fetch_all("""
        SELECT c.table_schema, c.table_name, c.column_name, c.data_type,
               c.is_nullable, t.table_type,
               col_description(format('%s.%s', c.table_schema, c.table_name)::regclass,
                               c.ordinal_position) AS column_comment
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema IN ('mart', 'meta')
        ORDER BY c.table_schema, c.table_name, c.ordinal_position
    """)

    metric_deps: dict[str, list[str]] = {}
    for r in db.fetch_all("""
        SELECT unnest(source_tables) AS obj, metric_key
        FROM meta.metric_definition WHERE is_active
    """):
        metric_deps.setdefault(r["obj"], []).append(r["metric_key"])

    counts = {}
    for r in db.fetch_all("""
        SELECT table_schema, table_name FROM information_schema.tables
        WHERE table_schema IN ('mart','meta') AND table_type = 'BASE TABLE'
    """):
        key = f"{r['table_schema']}.{r['table_name']}"
        counts[key] = db.scalar(f'SELECT count(*) FROM "{r["table_schema"]}"."{r["table_name"]}"')

    tables: dict[str, dict] = {}
    for r in cols:
        key = f"{r['table_schema']}.{r['table_name']}"
        entry = tables.setdefault(key, {
            "object": key,
            "schema": r["table_schema"],
            "type": "view" if r["table_type"] == "VIEW" else "table",
            "rows": counts.get(key),
            "dependent_metrics": sorted(metric_deps.get(key, [])),
            "columns": [],
        })
        level, note = classify(r["column_name"])
        entry["columns"].append({
            "column": r["column_name"],
            "type": r["data_type"],
            "nullable": r["is_nullable"] == "YES",
            "pii_class": level,
            "handling": note or None,
            "description": r["column_comment"],
        })

    personal = [
        (t["object"], c["column"], c["pii_class"])
        for t in tables.values() for c in t["columns"]
        if c["pii_class"] != "non_personal"
    ]

    return {
        "generated_at_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "disclosure": (
            "Every row in this warehouse is synthetic, generated by a seeded "
            "reproducible generator. No real guest, employee or booking is "
            "represented. Columns are nonetheless classified as if the data were "
            "real, because classifying realistic columns as safe on the grounds "
            "that the values are fake is the habit that leaks a production extract."
        ),
        "pii_rules": [
            {"pattern": p, "classification": lvl, "handling": note}
            for p, lvl, note in PII_RULES
        ],
        "summary": {
            "objects": len(tables),
            "tables": sum(1 for t in tables.values() if t["type"] == "table"),
            "views": sum(1 for t in tables.values() if t["type"] == "view"),
            "columns": sum(len(t["columns"]) for t in tables.values()),
            "columns_classified_personal": len(personal),
        },
        "personal_data_inventory": [
            {"object": o, "column": c, "classification": k} for o, c, k in sorted(personal)
        ],
        "objects": sorted(tables.values(), key=lambda t: t["object"]),
    }


def write_catalog_markdown(cat: dict, edge_count: int) -> None:
    lines = [
        "# Data catalog and lineage",
        "",
        f"_Generated {cat['generated_at_utc']} by `scripts/build_lineage_catalog.py`. "
        "Machine-readable copy: `reports/data_catalog.json`._",
        "",
        "## Synthetic data disclosure",
        "",
        cat["disclosure"],
        "",
        "## Summary",
        "",
        "| | |",
        "|---|---:|",
        f"| Objects catalogued | {cat['summary']['objects']} |",
        f"| Tables | {cat['summary']['tables']} |",
        f"| Views | {cat['summary']['views']} |",
        f"| Columns | {cat['summary']['columns']} |",
        f"| Columns classified as personal data | {cat['summary']['columns_classified_personal']} |",
        f"| Lineage edges | {edge_count} |",
        "",
        "## Lineage",
        "",
        "Most of this graph is **extracted, not declared**, which is the difference",
        "between lineage that stays true and lineage that rots:",
        "",
        "| Edge class | Source | Can it drift? |",
        "|---|---|---|",
        "| view → view, view → table | `pg_depend` / `pg_rewrite` | No — it is PostgreSQL's own dependency graph |",
        "| metric → source object | `meta.metric_definition.source_tables` | No — the column is mandatory and constrained |",
        "| source system → mart | declared | Yes — the generator runs outside the database |",
        "| mart → dashboard | declared | Yes — API and BI consumers are outside the database |",
        "",
        "Declared edges are labelled `declared:` in `transform_note` so a reader can",
        "separate extracted fact from authored claim.",
        "",
        "```",
        "seeded generator",
        "      |",
        "      v",
        "  mart.fact_*  mart.dim_*          (star schema, 14 tables)",
        "      |",
        "      +--> mart.v_unit_night_enriched --> mart.v_daily_kpi",
        "      |                                        |",
        "      +--> mart.v_booking_night ---------------+--> meta.metric_definition",
        "      |          |                                        |",
        "      |          +--> mart.v_pickup_daily                 v",
        "      |          +--> mart.v_booking_curve         API / Power BI / Excel",
        "      |          +--> mart.v_grain_reconciliation",
        "      +--> mart.v_cancellation_funnel",
        "      +--> mart.v_lead_time_profile",
        "```",
        "",
        "## Personal data inventory",
        "",
        "Classified by published rule, not by judgement. The rules are conservative:",
        "a column matching a known-identifier pattern is treated as personal data",
        "whether or not this synthetic dataset happens to hold anything sensitive.",
        "",
        "| Object | Column | Classification |",
        "|---|---|---|",
    ]
    for p in cat["personal_data_inventory"]:
        lines.append(f"| `{p['object']}` | `{p['column']}` | {p['classification']} |")

    lines += [
        "",
        "### Handling rules",
        "",
        "| Classification | Handling |",
        "|---|---|",
    ]
    seen = set()
    for rule in cat["pii_rules"]:
        if rule["classification"] in seen:
            continue
        seen.add(rule["classification"])
        lines.append(f"| `{rule['classification']}` | {rule['handling']} |")

    lines += [
        "",
        "**No API endpoint returns a raw guest record.** Every route is aggregate,",
        "the only free text exposed is a short evidence span quoted from a review,",
        "and a test scans all responses for credential values on every run.",
        "",
        "## Objects",
        "",
        "| Object | Type | Rows | Columns | Dependent metrics |",
        "|---|---|---:|---:|---|",
    ]
    for o in cat["objects"]:
        rows = f"{o['rows']:,}" if o["rows"] is not None else "—"
        metrics = ", ".join(f"`{m}`" for m in o["dependent_metrics"]) or "—"
        lines.append(
            f"| `{o['object']}` | {o['type']} | {rows} | {len(o['columns'])} | {metrics} |"
        )
    lines.append("")

    (REPORTS / "DATA_CATALOG.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    REPORTS.mkdir(exist_ok=True)
    print("Lineage and catalog")

    extracted = extracted_edges()
    metrics = metric_edges()
    declared = declared_edges()
    total = write_edges(extracted + metrics + declared)
    print(f"  lineage: {len(extracted)} extracted from pg_depend, "
          f"{len(metrics)} from the metric registry, {len(declared)} declared "
          f"-> {total} rows in meta.lineage_edge")

    cat = build_catalog()
    (REPORTS / "data_catalog.json").write_text(
        json.dumps(cat, indent=2, default=str), encoding="utf-8"
    )
    write_catalog_markdown(cat, total)
    print(f"  catalog: {cat['summary']['objects']} objects, "
          f"{cat['summary']['columns']} columns, "
          f"{cat['summary']['columns_classified_personal']} classified personal")
    print("  wrote DATA_CATALOG.md and data_catalog.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
