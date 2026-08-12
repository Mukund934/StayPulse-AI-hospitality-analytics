"""Generate a paste-ready Power Query loader and a DAX measure script.

These automate the two tedious parts of assembling the Power BI report: typing 12
tables by hand, and retyping ~35 measures. Both are plain text, so they are
diffable, reviewable and cannot silently drift from the warehouse.

Deliberately NOT generated: a .pbix or .pbip. Those are binary or schema-fussy
formats that cannot be opened and verified from a script, and shipping an
unverified project file is worse than shipping none - "I generated this but never
opened it" does not survive an interview.

Usage:
    python scripts/build_powerbi_scripts.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "powerbi" / "data"
OUT = ROOT / "powerbi" / "model"

# Power Query type inference from the column name. Explicit beats auto-detected:
# auto-detection is locale-sensitive, and a model that changes answer by laptop is
# not a model.
INT_HINTS = ("_key", "rooms_", "nights", "qty", "count", "score", "minutes",
             "reopened", "year", "quarter", "day_of", "week_of", "date_key",
             "sqft", "floor", "max_occupancy", "bedrooms", "unit_count", "adults",
             "lead_time_days", "rows_checked", "rows_failed", "settlement_days",
             "sla_minutes", "month")
DEC_HINTS = ("_inr", "_pct", "rate", "adr", "revpar", "commission_pct", "rating",
             "failure_pct", "avg_", "_gap_pp")
DATE_HINTS = ("date", "_on", "full_date", "same_day_last_year")
BOOL_HINTS = ("is_", "has_", "passed", "_verified")


def pq_type(col: str) -> str:
    c = col.lower()
    if c.endswith("_at") or c == "checked_at":
        return "type datetimezone"
    if any(c.startswith(h) for h in BOOL_HINTS) or c in ("passed",):
        return "type logical"
    if any(h in c for h in DATE_HINTS) and "key" not in c:
        return "type date"
    if any(h in c for h in DEC_HINTS):
        return "type number"
    if any(h in c for h in INT_HINTS):
        return "Int64.Type"
    return "type text"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tables = sorted(p.stem for p in DATA.glob("*.csv"))
    if not tables:
        print("no CSVs found — run scripts/export_bi_model.py first")
        return 1

    queries = []
    for t in tables:
        with open(DATA / f"{t}.csv", encoding="utf-8") as f:
            cols = next(csv.reader(f))
        typed = ", ".join('{"%s", %s}' % (c, pq_type(c)) for c in cols)
        queries.append(
            "  // -- %s (%d columns) --\n"
            "  %s =\n"
            "    let\n"
            "      Src     = Csv.Document(\n"
            "                  File.Contents(DataFolder & \"%s.csv\"),\n"
            "                  [Delimiter = \",\", Encoding = 65001, "
            "QuoteStyle = QuoteStyle.Csv]),\n"
            "      Headers = Table.PromoteHeaders(Src, [PromoteAllScalars = true]),\n"
            "      Typed   = Table.TransformColumnTypes(Headers, {%s})\n"
            "    in\n"
            "      Typed" % (t, len(cols), t, t, typed)
        )

    default_folder = str(DATA).replace("\\", "\\\\") + "\\\\"
    header = [
        "// StayPulse - Power Query loader for the exported star schema.",
        "//",
        "// HOW TO USE",
        "//   1. Power BI Desktop -> Home -> Transform data -> Advanced Editor",
        "//   2. Paste this whole script over the contents of a new blank query.",
        "//   3. Point DataFolder at your local powerbi/data path (keep the trailing slash).",
        "//   4. Close & Apply, then reference each table from its own blank query:",
        "//        = LoadAll[%s]" % tables[0],
        "//      or right-click the returned record -> Add as New Query.",
        "//",
        "// WHY A SCRIPT AND NOT THE FOLDER CONNECTOR",
        "//   The folder connector combines files that share ONE shape. These are %d" % len(tables),
        "//   DIFFERENT tables, so each needs its own explicit type contract. Types are",
        "//   declared by hand rather than auto-detected because auto-detection is",
        "//   locale-sensitive: 03/04 is 3 April or 4 March depending on the machine.",
        "//",
        "// Regenerate the CSVs any time with:  python scripts/export_bi_model.py",
        "",
        "let",
        "  // ---- CHANGE THIS ONE LINE ----",
        '  DataFolder = "%s",' % default_folder,
        "",
    ]
    body = ",\n\n".join(queries)
    footer = [
        "",
        "  LoadAll = [",
        ",\n".join("    %s = %s" % (t, t) for t in tables),
        "  ]",
        "in",
        "  LoadAll",
        "",
    ]
    m = "\n".join(header) + body + "\n" + "\n".join(footer)
    (OUT / "load.pq").write_text(m, encoding="utf-8")
    print("  load.pq        %d tables, %s bytes" % (len(tables), f"{len(m):,}"))

    # measures.dax is extracted from powerbi/README.md so there is ONE source of
    # truth for the DAX. Editing it in two places is how they diverge.
    readme = (ROOT / "powerbi" / "README.md").read_text(encoding="utf-8")
    if "```dax" not in readme:
        print("  measures.dax   SKIPPED - no dax block found in powerbi/README.md")
        return 1
    block = readme.split("```dax", 1)[1].split("```", 1)[0].strip()
    n = sum(1 for ln in block.splitlines()
            if ln.strip() and not ln.strip().startswith("--") and "=" in ln
            and not ln.strip().startswith(("VAR", "RETURN")))

    dax = "\n".join([
        "-- StayPulse - all model measures, paste-ready.",
        "--",
        "-- HOW TO USE",
        "--   Power BI Desktop -> Home -> Enter data -> create an empty table named",
        "--   _Measures, then add each measure below. Or Tabular Editor ->",
        "--   Advanced Scripting.",
        "--",
        "-- WHY EXPLICIT MEASURES",
        "--   Never drag a numeric column onto a visual and let Power BI implicitly",
        "--   sum it. Implicit aggregation is how two visuals on one page end up",
        "--   disagreeing, and it bypasses the definitions registered in",
        "--   meta.metric_definition.",
        "--",
        "-- DEFINITION PARITY",
        "--   Every measure mirrors the SQL in meta.metric_definition. Change one,",
        "--   change both, then run: python scripts/validate_metrics.py",
        "--   That script recomputes every published figure independently of the views",
        "--   and compares, so a divergence fails loudly instead of shipping.",
        "",
        block,
        "",
    ])
    (OUT / "measures.dax").write_text(dax, encoding="utf-8")
    print("  measures.dax   ~%d measures, %s bytes" % (n, f"{len(dax):,}"))

    # Balance checks. Not a parser, but it catches the truncation and copy errors
    # that actually happen.
    ok = True
    for name, txt, pairs in (("load.pq", m, "(){}[]"), ("measures.dax", dax, "()")):
        for a, b in zip(pairs[::2], pairs[1::2]):
            if txt.count(a) != txt.count(b):
                print("  BALANCE FAIL %s: %s=%d %s=%d"
                      % (name, a, txt.count(a), b, txt.count(b)))
                ok = False
    print("  balance check  %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
