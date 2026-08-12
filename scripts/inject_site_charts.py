"""Inject generated SVG charts into web/index.html at fixed anchors.

Idempotent: existing injected blocks are replaced, so this can run on every build
without the page accumulating duplicates.

Usage:
    python scripts/build_site_charts.py && python scripts/inject_site_charts.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "index.html"
CHARTS = ROOT / "web" / "charts.html"

# Where each chart goes: (chart key, the exact anchor line it is inserted AFTER)
PLACEMENTS = [
    ("revenue_trend", '  <div class="callout ok">\n    <span class="h">The assertion that matters</span>'),
    ("sla_heatmap",   '<section id="findings">'),
    ("channel_mix",   '<section id="metrics">'),
    ("ai_benchmark",  '<section id="ai">'),
    ("dq",            '<section id="quality">'),
]

CSS = """
/* charts — injected */
figure.chart{margin:0 0 22px;border:1px solid var(--rule);border-radius:8px;
  background:var(--paper);padding:16px 18px 12px;overflow:hidden}
figure.chart svg{width:100%;height:auto;display:block;margin-top:12px;overflow:visible}
figure.chart figcaption{font-size:14px;color:var(--ink2)}
figure.chart figcaption strong{display:block;font-size:15px;color:var(--ink);
  margin-bottom:5px;font-weight:650}
figure.chart figcaption span{display:block;font-size:13px;color:var(--ink3);
  line-height:1.5;max-width:78ch}
"""


def main() -> int:
    if not CHARTS.exists():
        print("charts.html missing — run build_site_charts.py first")
        return 1

    raw = CHARTS.read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    for part in raw.split("<!--CHART:")[1:]:
        key, _, body = part.partition("-->")
        blocks[key.strip()] = body.strip()

    page = PAGE.read_text(encoding="utf-8")

    # Remove any previously injected charts so this is idempotent.
    page = re.sub(r"\n<!-- chart:[a-z_]+ -->.*?<!-- /chart -->", "", page, flags=re.S)

    injected = 0
    for key, anchor in PLACEMENTS:
        if key not in blocks:
            print(f"  MISSING chart: {key}")
            continue
        if anchor not in page:
            print(f"  ANCHOR NOT FOUND for {key}: {anchor[:60]!r}")
            continue
        wrapped = f"\n<!-- chart:{key} -->\n{blocks[key]}\n<!-- /chart -->"
        # Section anchors take the chart AFTER the heading block; the callout
        # anchor takes it BEFORE, so the trend sits above the identity note.
        if anchor.startswith("<section"):
            idx = page.index(anchor) + len(anchor)
            # skip past the <span class="tag"> and <h2> that follow a section open
            m = re.compile(r"</h2>").search(page, idx)
            idx = m.end() if m else idx
            page = page[:idx] + wrapped + page[idx:]
        else:
            idx = page.index(anchor)
            page = page[:idx] + wrapped.lstrip("\n") + "\n" + page[idx:]
        injected += 1

    if "figure.chart" not in page:
        page = page.replace("</style>", CSS + "</style>", 1)

    PAGE.write_text(page, encoding="utf-8")
    size = PAGE.stat().st_size
    print(f"  injected {injected}/{len(PLACEMENTS)} charts")
    print(f"  page size: {size:,} bytes")
    return 0 if injected == len(PLACEMENTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
