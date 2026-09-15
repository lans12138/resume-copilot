"""Review-screen read routes: wiring and authentication (FIN-004, hermetic).

The review route is document-centric ("/documents/:documentId/review") because a
resume is reviewed in the talent pool, before it belongs to any job. That means
the reads it needs cannot be job-scoped. These tests pin the three document- and
profile-scoped read endpoints — plus the fact that they are rejected before any
database or broker access when the caller is unauthenticated.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.main import create_app
from tests.unit.settings_factory import make_settings

_DOCUMENT_CONTENT = "/api/v1/documents/{document_id}/content"
_PROFILE_BY_DOCUMENT = "/api/v1/candidate-profiles/by-document/{document_id}"
_PROFILE_EVIDENCE = "/api/v1/candidate-profiles/{profile_id}/evidence"

_EXPECTED_METHODS = {
    _DOCUMENT_CONTENT: ("get",),
    _PROFILE_BY_DOCUMENT: ("get",),
    _PROFILE_EVIDENCE: ("get",),
}

_IDS = {
    "document_id": "00000000-0000-0000-0000-000000000001",
    "profile_id": "00000000-0000-0000-0000-000000000002",
}


def test_review_read_endpoints_registered() -> None:
    paths = create_app(make_settings()).openapi()["paths"]
    for path, methods in _EXPECTED_METHODS.items():
        assert path in paths, path
        for method in methods:
            assert method in paths[path], (method, path)


def test_review_read_endpoints_require_authentication() -> None:
    client = TestClient(create_app(make_settings()))
    for path, methods in _EXPECTED_METHODS.items():
        url = path.format(**_IDS)
        for method in methods:
            response = getattr(client, method)(url)
            assert response.status_code in (401, 403), (method, url, response.status_code)


def test_writes_stay_job_scoped_while_reads_are_document_scoped() -> None:
    """Guard the FIN-004 decision so a later "cleanup" cannot undo it.

    The write paths (confirm / evidence pinning) keep the job-level resource
    authorization. The reads deliberately do not carry job scope, otherwise the
    document-first review route could not call them at all.
    """
    paths = create_app(make_settings()).openapi()["paths"]

    assert "/api/v1/jobs/{job_id}/profiles/{profile_id}/confirm" in paths
    assert "post" in paths["/api/v1/jobs/{job_id}/profiles/{profile_id}/evidence"]

    for path in _EXPECTED_METHODS:
        assert path in paths
        assert not path.startswith("/api/v1/jobs/"), path
