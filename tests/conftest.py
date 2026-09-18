"""Session-wide test isolation from the developer's local configuration.

``Settings`` declares ``env_file=".env"``, which pydantic-settings resolves
*relative to the current working directory*. That made the configuration tests
depend on where pytest happened to be started from: running the suite from a
checkout that has a real ``.env`` produced different results than running it from
an isolated directory (PORT-001). A test outcome must not depend on a file that
is gitignored and only exists on one machine.

The fixture below makes that isolation explicit rather than incidental. It stops
``Settings`` from reading ``.env`` for the whole session, so a developer's local
secrets and overrides can never change what the suite proves.

Ambient environment variables are deliberately *not* stripped: the integration
suites use ``DATABASE_URL`` as an explicit, opt-in contract to decide whether to
run against a real PostgreSQL, and silently deleting it would turn those tests
into permanent skips — a green suite that proves less than it claims.
"""

from __future__ import annotations

import pytest

from backend.app.core.settings import Settings


@pytest.fixture(autouse=True)
def isolate_settings_from_local_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent any test from reading the checkout's local ``.env``."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
