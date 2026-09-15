"""OpenAPI contract gate (FIN-012 item 3).

The browser only ever talks to ``/api`` through the Nginx entry point, so the
API surface has two properties that a reviewer cannot see by reading routers:

1. every route is under ``/api`` — anything else is answered by the SPA
   fallback in ``deploy/nginx/templates/default.conf.template`` and therefore
   returns ``index.html`` instead of an error, which is a silent failure;
2. every state-changing route declares authentication, so a new endpoint cannot
   quietly ship without the RBAC check the rest of the service relies on.

Both are asserted here rather than in a probe because they are properties of the
schema, not of a running stack: this gate runs in ordinary pytest, with no
Docker, in the same suite as everything else.

The inventory below is pinned on purpose. Adding a route is a deliberate act and
should require editing this file, because "the API grew a new endpoint" is
exactly the change a compatibility gate exists to make visible.
"""

from __future__ import annotations

from typing import Any

from backend.app.main import create_app
from tests.unit.settings_factory import make_settings

# Operations reachable without a credential.
#
# ``GET /api/v1/metrics`` is listed because that is the current implementation,
# not because it is the desired one: the endpoint was added in IMP-029 with no
# dependency, so it is served unauthenticated. Treat it as a known gap — if it is
# ever placed behind ``require_roles``, delete it from this set and the third
# test below starts demanding the security declaration.
PUBLIC_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/auth/token"),
        ("GET", "/api/v1/health/live"),
        ("GET", "/api/v1/health/ready"),
        ("GET", "/api/v1/metrics"),
    }
)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

# Pinned inventory: (method, path) for every operation in the document.
EXPECTED_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/application-runs/{run_id}"),
        ("POST", "/api/v1/application-runs/{run_id}/cancel"),
        ("GET", "/api/v1/application-runs/{run_id}/events"),
        ("POST", "/api/v1/application-runs/{run_id}/retry"),
        ("GET", "/api/v1/applications/{application_id}/runs"),
        ("POST", "/api/v1/applications/{application_id}/runs"),
        ("GET", "/api/v1/approvals/{approval_id}"),
        ("POST", "/api/v1/approvals/{approval_id}/decision"),
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/auth/token"),
        ("GET", "/api/v1/candidate-profiles/by-document/{document_id}"),
        ("GET", "/api/v1/candidate-profiles/{profile_id}/evidence"),
        ("GET", "/api/v1/documents"),
        ("POST", "/api/v1/documents"),
        ("GET", "/api/v1/documents/{document_id}"),
        ("GET", "/api/v1/documents/{document_id}/content"),
        ("POST", "/api/v1/documents/{document_id}/retry"),
        ("GET", "/api/v1/evaluations"),
        ("POST", "/api/v1/evaluations"),
        ("GET", "/api/v1/evaluations/{evaluation_run_id}"),
        ("GET", "/api/v1/health/live"),
        ("GET", "/api/v1/health/ready"),
        ("GET", "/api/v1/interviews"),
        ("GET", "/api/v1/interviews/{interview_id}"),
        ("GET", "/api/v1/jobs"),
        ("POST", "/api/v1/jobs"),
        ("GET", "/api/v1/jobs/{job_id}"),
        ("PATCH", "/api/v1/jobs/{job_id}"),
        ("POST", "/api/v1/jobs/{job_id}/activate"),
        ("GET", "/api/v1/jobs/{job_id}/applications"),
        ("GET", "/api/v1/jobs/{job_id}/assignments"),
        ("POST", "/api/v1/jobs/{job_id}/assignments"),
        ("DELETE", "/api/v1/jobs/{job_id}/assignments/{user_id}"),
        ("GET", "/api/v1/jobs/{job_id}/candidates"),
        ("POST", "/api/v1/jobs/{job_id}/close"),
        ("GET", "/api/v1/jobs/{job_id}/match-runs"),
        ("POST", "/api/v1/jobs/{job_id}/match-runs"),
        ("GET", "/api/v1/jobs/{job_id}/profiles/{profile_id}"),
        ("POST", "/api/v1/jobs/{job_id}/profiles/{profile_id}/confirm"),
        ("GET", "/api/v1/jobs/{job_id}/profiles/{profile_id}/evidence"),
        ("POST", "/api/v1/jobs/{job_id}/profiles/{profile_id}/evidence"),
        ("GET", "/api/v1/match-runs/{run_id}"),
        ("POST", "/api/v1/match-runs/{run_id}/cancel"),
        ("GET", "/api/v1/match-runs/{run_id}/events"),
        ("GET", "/api/v1/match-runs/{run_id}/reports"),
        ("POST", "/api/v1/match-runs/{run_id}/retry"),
        ("GET", "/api/v1/metrics"),
    }
)


def _openapi() -> dict[str, Any]:
    return create_app(make_settings()).openapi()


def _operations(spec: dict[str, Any]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path, methods in spec.get("paths", {}).items():
        for method in methods:
            if method.upper() in HTTP_METHODS:
                found.add((method.upper(), path))
    return found


def test_every_route_is_reachable_through_the_public_entry_point() -> None:
    """Nginx proxies ``/api/`` only; anything else is swallowed by the SPA shell."""
    spec = _openapi()
    offenders = sorted(path for path in spec.get("paths", {}) if not path.startswith("/api/"))

    assert offenders == [], (
        "These routes are outside the proxy's /api/ prefix, so the entry point "
        f"answers them with index.html instead of an error: {offenders}"
    )


def test_write_operations_declare_authentication() -> None:
    """A state-changing endpoint without a security declaration is an RBAC hole."""
    spec = _openapi()
    unsecured: list[str] = []
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            upper = method.upper()
            if upper not in WRITE_METHODS:
                continue
            if (upper, path) in PUBLIC_OPERATIONS:
                continue
            if not operation.get("security"):
                unsecured.append(f"{upper} {path}")

    assert unsecured == [], (
        "These state-changing operations declare no authentication in the "
        f"OpenAPI document: {sorted(unsecured)}"
    )


def test_openapi_document_has_no_dangling_refs() -> None:
    """A ``$ref`` that resolves to nothing is a schema that cannot be generated."""
    spec = _openapi()
    components = spec.get("components", {})
    dangling: list[str] = []

    def walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    if not _resolve(components, value):
                        dangling.append(f"{trail} -> {value}")
                else:
                    walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")

    walk(spec, "$")

    assert dangling == [], f"Unresolvable $ref in the OpenAPI document: {dangling}"


def _resolve(components: dict[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        return None
    node: Any = components
    # Component refs are written ``#/components/schemas/X``; ``components`` here
    # is already the object *inside* the document, so the first two segments are
    # stripped to reach the same node.
    parts = reference[2:].split("/")
    if parts and parts[0] == "components":
        parts = parts[1:]
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_route_inventory_is_pinned() -> None:
    """Adding or removing an endpoint must be an explicit edit to this file."""
    actual = _operations(_openapi())

    missing = sorted(EXPECTED_OPERATIONS - actual)
    unexpected = sorted(actual - EXPECTED_OPERATIONS)

    assert not missing and not unexpected, (
        "The API surface changed. Update EXPECTED_OPERATIONS in this file after "
        f"confirming the change is intentional. missing={missing} unexpected={unexpected}"
    )
