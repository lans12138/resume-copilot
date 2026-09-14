"""FIN-003 candidate profile/evidence router wiring tests (hermetic).

Confirms the four new HR-facing endpoints are registered and gated by the auth
dependency, without touching a database or broker. The business logic
(confirm / optimistic lock / single-READY / cross-document evidence) is covered
by ``test_evidence_and_profile_review.py``; the full upload→parse→confirm→
embedding flow against real Postgres+Redis is the integration test.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.main import create_app
from tests.unit.settings_factory import make_settings

_EXPECTED_PATHS = (
    "/api/v1/jobs/{job_id}/profiles/{profile_id}",
    "/api/v1/jobs/{job_id}/profiles/{profile_id}/confirm",
    "/api/v1/jobs/{job_id}/profiles/{profile_id}/evidence",
)


def test_profile_endpoints_registered_in_openapi() -> None:
    paths = create_app(make_settings()).openapi()["paths"]
    for path in _EXPECTED_PATHS:
        assert path in paths, f"missing route {path}"
    # GET profile + GET evidence are GET; confirm + create-evidence are POST.
    assert "get" in paths[_EXPECTED_PATHS[0]]
    assert "post" in paths[_EXPECTED_PATHS[1]]
    assert "get" in paths[_EXPECTED_PATHS[2]]
    assert "post" in paths[_EXPECTED_PATHS[3]]


def test_profile_endpoints_require_authentication() -> None:
    client = TestClient(create_app(make_settings()))
    for path in _EXPECTED_PATHS:
        # job_id/profile_id are path params; use placeholder uuids.
        url = path.format(job_id="00000000-0000-0000-0000-000000000001",
                          profile_id="00000000-0000-0000-0000-000000000002")
        for method in ("get", "post"):
            response = getattr(client, method)(url)
            # Unauthenticated must be rejected before any DB/broker access.
            assert response.status_code in (401, 403), (method, url, response.status_code)
