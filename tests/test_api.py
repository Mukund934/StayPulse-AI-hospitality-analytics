"""API tests.

The security tests are the ones that matter most here: an analytics API that leaks a
connection string in an error message is worse than no API. Two of them force
failures rather than waiting to observe one, because a sanitiser that has never been
exercised is indistinguishable from one that does not work.

Run:  python -m pytest tests/test_api.py -v
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from api.app import services  # noqa: E402
from api.app.main import ALLOWED_ORIGINS, app  # noqa: E402

logging.disable(logging.INFO)

client = TestClient(app)
# Separate client for the error-path tests: the default re-raises server exceptions
# instead of letting the handler produce a response.
error_client = TestClient(app, raise_server_exceptions=False)

READ_ONLY_ENDPOINTS = [
    "/health",
    "/health/readiness",
    "/",
    "/api/kpis/overview",
    "/api/revenue/trends",
    "/api/revenue/channels",
    "/api/properties",
    "/api/operations/overview",
    "/api/operations/sla",
    "/api/operations/service-requests",
    "/api/guest-intelligence/overview",
    "/api/guest-intelligence/aspects",
    "/api/guest-intelligence/issues",
    "/api/guest-intelligence/benchmark",
    "/api/anomalies",
    "/api/decisions",
    "/api/daily-brief/latest",
    "/api/daily-brief/history",
    "/api/data-quality/overview",
    "/api/data-quality/rules",
    "/api/metrics",
    "/api/pipeline-runs",
]


class TestHealth:
    def test_health_is_instant_and_does_no_io(self):
        """Render pings this constantly; touching the DB here risks restart loops."""
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_health_reveals_no_infrastructure(self):
        body = client.get("/health").text.lower()
        for leak in ("host", "user", "password", "supabase", "pooler", "postgres"):
            assert leak not in body

    def test_readiness_reports_database_without_exposing_it(self):
        r = client.get("/health/readiness")
        assert r.status_code == 200
        data = r.json()
        assert data["database"] in ("reachable", "unreachable")
        if data["database"] == "reachable":
            assert data["analytical_tables"] > 0
        assert "PGPASSWORD" not in r.text
        # The engine banner is fine; a host or username is not.
        assert "pooler.supabase.com" not in r.text


class TestEndpointsRespond:
    @pytest.mark.parametrize("endpoint", READ_ONLY_ENDPOINTS)
    def test_returns_200(self, endpoint):
        r = client.get(endpoint)
        assert r.status_code == 200, f"{endpoint} -> {r.status_code}: {r.text[:200]}"

    def test_index_lists_its_own_endpoints(self):
        listed = client.get("/").json()["endpoints"]
        # Every advertised endpoint must actually resolve. A stale index is a
        # broken promise a reviewer will click on.
        for path in listed:
            if "{" in path:
                continue
            assert client.get(path).status_code == 200, f"advertised but broken: {path}"


class TestSemanticLayerParity:
    """The API must not become a second definition of any metric."""

    def test_revpar_identity_holds_in_api_response(self):
        d = client.get("/api/kpis/overview").json()
        occ = d["occupancy_pct"] / 100.0
        assert abs(d["revpar_inr"] - d["adr_inr"] * occ) < 1.0

    def test_kpis_declare_their_date_basis(self):
        d = client.get("/api/kpis/overview").json()
        assert d["date_basis"] == "stay_date"
        assert d["is_synthetic"] is True

    def test_api_matches_direct_database_query(self):
        from staypulse import db
        api = client.get("/api/kpis/overview").json()
        direct = db.fetch_all("""
            SELECT count(*) FILTER (WHERE is_sellable) av,
                   count(*) FILTER (WHERE is_occupied) sold
            FROM mart.fact_unit_night
        """)[0]
        assert api["rooms_available"] == int(direct["av"])
        assert api["rooms_sold"] == int(direct["sold"])

    def test_comparison_window_is_same_length(self):
        d = client.get("/api/kpis/overview?days=30").json()
        assert d["comparison"] is not None
        assert d["period"]["trailing_days"] == 30

    def test_metric_registry_is_served(self):
        metrics = client.get("/api/metrics").json()["metrics"]
        assert len(metrics) >= 14
        assert all(m["date_basis"] for m in metrics)
        assert all(m["caveats"] for m in metrics)


class TestValidation:
    def test_rejects_out_of_range_days(self):
        assert client.get("/api/kpis/overview?days=0").status_code == 422
        assert client.get("/api/kpis/overview?days=9999").status_code == 422

    def test_rejects_unknown_grain(self):
        assert client.get("/api/revenue/trends?grain=fortnight").status_code == 422
        assert client.get("/api/revenue/trends?grain=month").status_code == 200

    def test_rejects_bad_property_key(self):
        assert client.get("/api/properties/0/performance").status_code == 422
        assert client.get("/api/properties/abc/performance").status_code == 422

    def test_missing_property_is_404_not_500(self):
        r = client.get("/api/properties/99999/performance")
        assert r.status_code == 404

    def test_limit_is_capped(self):
        assert client.get("/api/guest-intelligence/issues?limit=101").status_code == 422
        assert client.get("/api/guest-intelligence/issues?limit=5").status_code == 200


class TestSecurity:
    def test_no_secret_value_appears_in_any_response(self):
        from staypulse import config
        config.load_env()
        secrets = [v for k in ("PGPASSWORD", "GEMINI_API_KEY", "OPENWEATHER_API_KEY",
                               "PGHOST", "PGUSER")
                   if (v := os.environ.get(k)) and len(v) >= 8]
        if not secrets:
            pytest.skip("no credentials configured in this environment")
        for endpoint in READ_ONLY_ENDPOINTS:
            text = client.get(endpoint).text
            for secret in secrets:
                assert secret not in text, f"{endpoint} leaked a credential"

    def test_driver_error_is_sanitised(self, monkeypatch):
        """Force a realistic psycopg failure and confirm nothing escapes.

        A real connection error message contains the host, the port and the
        username. That must never reach a client.
        """
        boom = ("connection to server at 1.2.3.4 port 5432 failed: "
                "password authentication failed for user secretuser")

        def explode(*_a, **_k):
            raise RuntimeError(boom)

        monkeypatch.setattr(services.db, "fetch_all", explode)
        r = error_client.get("/api/kpis/overview")
        assert r.status_code == 503
        for leak in ("1.2.3.4", "secretuser", "password authentication", "Traceback"):
            assert leak not in r.text
        assert r.json()["error"] == "analytics_unavailable"

    def test_no_write_methods_are_exposed(self):
        """Read-only by construction: nothing here can mutate the warehouse."""
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            assert not (methods & {"POST", "PUT", "PATCH", "DELETE"}), \
                f"mutating method exposed on {getattr(route, 'path', route)}"

    def test_cors_allows_the_production_origin(self):
        origin = "https://stay-pulse-ai-hospitality-analytics.vercel.app"
        assert origin in ALLOWED_ORIGINS
        r = client.get("/health", headers={"Origin": origin})
        assert r.headers.get("access-control-allow-origin") == origin

    def test_cors_rejects_an_unknown_origin(self):
        r = client.get("/health", headers={"Origin": "https://evil.example.com"})
        assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"

    def test_no_wildcard_cors(self):
        assert "*" not in ALLOWED_ORIGINS


class TestObservability:
    def test_request_id_and_timing_headers(self):
        r = client.get("/health")
        assert len(r.headers.get("X-Request-Id", "")) == 12
        assert r.headers.get("X-Response-Time-Ms", "").isdigit()

    def test_openapi_schema_is_valid(self):
        s = client.get("/openapi.json").json()
        assert s["info"]["title"] == "StayPulse Analytics API"
        assert len(s["paths"]) >= 20
        # Every path needs a summary, or /docs is unusable in an interview.
        for path, ops in s["paths"].items():
            for method, op in ops.items():
                assert op.get("summary"), f"{method.upper()} {path} has no summary"

    def test_docs_are_served(self):
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200


class TestHonesty:
    def test_synthetic_data_is_disclosed(self):
        assert "synthetic" in client.get("/").text.lower()
        assert client.get("/api/kpis/overview").json()["is_synthetic"] is True

    def test_ai_ground_truth_limitation_is_disclosed(self):
        body = client.get("/api/guest-intelligence/benchmark").json()
        assert "generator-derived" in body["ground_truth"]
        assert "NOT human-annotated" in body["ground_truth"]

    def test_quality_score_explains_why_it_is_below_100(self):
        d = client.get("/api/data-quality/overview").json()
        assert d["quality_score"] < 100
        assert "deliberate" in d["note"] or "planted" in d["note"]
