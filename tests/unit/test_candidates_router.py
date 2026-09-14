"""FIN-003 candidate profile/evidence router wiring tests (hermetic).

Confirms the new HR-facing endpoints are registered and gated by the auth
dependency, without touching a database or broker. The business logic
(confirm / optimistic lock / single-READY / cross-document evidence) is covered
by ``test_evidence_and_profile_review.py``; the full upload→parse→confirm→
embedding flow against real Postgres+Redis is the integration test.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.candidates.schemas import EvidenceChunkCreate, EvidenceLocator
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


def test_evidence_body_does_not_have_to_repeat_the_profile_the_path_owns() -> None:
    """The path binds the chunk to a profile, so the body must not be forced to.

    Requiring ``candidate_profile_id`` in the body while the route overwrote it with
    the path value made a correct client's pin request fail with 422 instead of
    reaching the service.
    """
    schemas = create_app(make_settings()).openapi()["components"]["schemas"]
    assert "candidate_profile_id" not in schemas["EvidenceChunkCreate"]["required"]

    chunk = EvidenceChunkCreate(
        document_id=uuid4(),
        chunk_index=0,
        section_type="技能",
        locator=EvidenceLocator(kind="docx_paragraph", paragraph_index=1, char_start=0, char_end=6),
        text="Python",
    )
    assert chunk.candidate_profile_id is None
