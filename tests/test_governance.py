"""Lineage, catalog and PII-exposure tests.

The catalog asserts in prose that no API route returns a raw guest record. Prose is
not a control. This file turns that claim into a test that scans every endpoint for
the names AND the actual values of every column the catalog classified as a direct
identifier, so the document and the running service cannot drift apart.

Run:  python -m pytest tests/test_governance.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from api.app.main import app  # noqa: E402
from staypulse import db  # noqa: E402

CATALOG = PROJECT_ROOT / "reports" / "data_catalog.json"
client = TestClient(app)


@pytest.fixture(scope="module")
def catalog() -> dict:
    if not CATALOG.exists():
        pytest.skip("catalog not generated; run scripts/build_lineage_catalog.py")
    return json.loads(CATALOG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def endpoints() -> list[str]:
    return [p for p in client.get("/").json()["endpoints"] if "{" not in p]


class TestLineageIsPopulated:
    def test_lineage_table_is_not_empty(self):
        """Regression guard for audit finding F-2: the table existed with 0 rows."""
        assert db.scalar("SELECT count(*) FROM meta.lineage_edge") > 50

    def test_most_edges_are_extracted_not_declared(self):
        """Hand-written lineage rots. Extracted lineage cannot."""
        total = db.scalar("SELECT count(*) FROM meta.lineage_edge")
        declared = db.scalar(
            "SELECT count(*) FROM meta.lineage_edge WHERE transform_note LIKE 'declared:%'"
        )
        assert (total - declared) / total > 0.6, (
            f"only {total - declared}/{total} edges are extracted from the database"
        )

    def test_every_view_has_an_upstream_edge(self):
        """A view with no lineage means the extraction missed it."""
        orphans = db.fetch_all("""
            SELECT 'mart.' || table_name AS obj
            FROM information_schema.tables
            WHERE table_schema = 'mart' AND table_type = 'VIEW'
              AND 'mart.' || table_name NOT IN (
                  SELECT target_object FROM meta.lineage_edge
              )
        """)
        assert not orphans, f"views with no upstream lineage: {[o['obj'] for o in orphans]}"

    def test_every_active_metric_traces_to_a_source(self):
        missing = db.fetch_all("""
            SELECT metric_key FROM meta.metric_definition m
            WHERE m.is_active AND NOT EXISTS (
                SELECT 1 FROM meta.lineage_edge e
                WHERE e.target_object = m.metric_key AND e.target_layer = 'metric'
            )
        """)
        assert not missing, f"metrics with no lineage: {[m['metric_key'] for m in missing]}"

    def test_declared_edges_are_labelled_as_such(self):
        """A reader must be able to separate extracted fact from authored claim."""
        rows = db.fetch_all("""
            SELECT transform_note FROM meta.lineage_edge
            WHERE source_layer = 'source_system' OR target_layer = 'dashboard'
        """)
        assert rows
        assert all(r["transform_note"].startswith("declared:") for r in rows)


class TestCatalogMatchesReality:
    def test_catalog_covers_every_analytical_object(self, catalog):
        catalogued = {o["object"] for o in catalog["objects"]}
        actual = {
            f"{r['table_schema']}.{r['table_name']}"
            for r in db.fetch_all("""
                SELECT table_schema, table_name FROM information_schema.tables
                WHERE table_schema IN ('mart','meta')
            """)
        }
        assert actual <= catalogued, f"missing from catalog: {actual - catalogued}"

    def test_row_counts_are_current(self, catalog):
        """A catalog with stale counts is a catalog nobody should trust."""
        sample = next(
            o for o in catalog["objects"]
            if o["object"] == "mart.fact_unit_night"
        )
        live = db.scalar("SELECT count(*) FROM mart.fact_unit_night")
        assert sample["rows"] == live

    def test_pii_rules_are_published_with_the_classification(self, catalog):
        assert catalog["pii_rules"]
        for rule in catalog["pii_rules"]:
            assert rule["pattern"] and rule["classification"] and rule["handling"]

    def test_synthetic_origin_is_disclosed(self, catalog):
        assert "synthetic" in catalog["disclosure"].lower()

    def test_known_identifiers_are_classified(self, catalog):
        """The rule must actually catch the columns everyone would expect."""
        found = {
            (p["object"], p["column"]) for p in catalog["personal_data_inventory"]
        }
        for expected in (
            ("mart.dim_guest", "email"),
            ("mart.dim_guest", "phone"),
            ("mart.dim_guest", "full_name"),
        ):
            assert expected in found, f"{expected} was not classified as personal data"


class TestNoPersonalDataLeavesTheApi:
    """The catalog's central claim, enforced rather than asserted."""

    def test_no_direct_identifier_column_name_appears_in_any_response(
        self, catalog, endpoints
    ):
        names = {
            p["column"] for p in catalog["personal_data_inventory"]
            if p["classification"] in ("direct_identifier", "sensitive_identifier")
        }
        assert names, "expected some direct identifiers in the catalog"
        for path in endpoints:
            body = client.get(path).text
            for column in names:
                assert f'"{column}"' not in body, (
                    f"{path} exposes a direct-identifier column named {column}"
                )

    def test_no_real_guest_contact_value_appears_in_any_response(self, endpoints):
        """Scan for the actual values, not just the column names.

        A route could rename `email` to `contact` and still leak it. This takes a
        sample of live identifier values out of the warehouse and greps every
        response for them.
        """
        rows = db.fetch_all("""
            SELECT full_name, email, phone FROM mart.dim_guest
            WHERE email IS NOT NULL AND phone IS NOT NULL
            ORDER BY guest_key LIMIT 40
        """)
        values = {
            str(v) for r in rows for v in r.values()
            if v and len(str(v)) >= 8
        }
        assert values, "no guest contact values available to test against"

        for path in endpoints:
            body = client.get(path).text
            for value in values:
                assert value not in body, f"{path} leaked a guest identifier value"

    def test_review_text_is_only_ever_a_short_evidence_span(self, endpoints):
        """Free text can carry incidental PII, so bulk export must not happen."""
        body = client.get("/api/guest-intelligence/issues?limit=25").json()
        for issue in body.get("issues", []):
            for key, value in issue.items():
                if isinstance(value, str) and key.lower().endswith(("text", "evidence", "quote")):
                    assert len(value) <= 400, (
                        f"{key} returned {len(value)} characters; evidence spans "
                        "should be short quotations, not whole reviews"
                    )
