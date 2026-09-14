"""FIN-003 candidate profile/evidence router wiring tests (hermetic).

Confirms the new HR-facing endpoints are registered and gated by the auth
dependency, without touching a database or broker. The business logic
(confirm / optimistic lock / single-READY / cross-document evidence) is covered
by ``test_evidence_and_profile_review.py``; the full upload→parse→confirm→
embedding flow against real Postgres+Redis is the integration test.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.main import create_app
from tests.unit.settings_factory import make_settings

# Only three distinct paths: the evidence path exposes both GET and POST.
_PROFILE = "/api/v1/jobs/{job_id}/profiles/{profile_id}"
_CONFIRM = "/api/v1/jobs/{job_id}/profiles/{profile_id}/confirm"
_EVIDENCE = "/api/v1/jobs/{job_id}/profiles/{profile_id}/evidence"

_EXPECTED_METHODS = {
    _PROFILE: ("get",),
    _CONFIRM: ("post",),
    _EVIDENCE: ("get", "post"),
}


def test_profile_endpoints_registered_in_openapi() -> None:
    paths = create_app(make_settings()).openapi()["paths"]
    assert _PROFILE in paths
    assert _CONFIRM in paths
    assert _EVIDENCE in paths
    assert "get" in paths[_PROFILE]
    assert "post" in paths[_CONFIRM]
    assert "get" in paths[_EVIDENCE]
    assert "post" in paths[_EVIDENCE]


def test_profile_endpoints_require_authentication() -> None:
    client = TestClient(create_app(make_settings()))
    for path, methods in _EXPECTED_METHODS.items():
        url = path.format(
            job_id="00000000-0000-0000-0000-000000000001",
            profile_id="00000000-0000-0000-0000-000000000002",
        )
        for method in methods:
            response = getattr(client, method)(url)
            # Unauthenticated must be rejected before any DB/broker access.
            assert response.status_code in (401, 403), (method, url, response.status_code)
