"""Bounded dependency readiness aggregation with safe public details."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pydantic import JsonValue

from backend.app.infrastructure.runtime import RuntimeResources

logger = logging.getLogger(__name__)

DependencyCheck = Callable[[], Awaitable[None]]


async def check_readiness(
    resources: RuntimeResources,
    *,
    timeout_seconds: float = 3.0,
) -> tuple[bool, dict[str, JsonValue]]:
    """Run independent dependency probes without exposing exception details."""
    checks: dict[str, DependencyCheck] = {
        "postgres": resources.check_postgres,
        "redis": resources.check_redis,
        "storage": resources.check_storage,
    }

    async def run_check(name: str, check: DependencyCheck) -> tuple[str, str]:
        try:
            async with asyncio.timeout(timeout_seconds):
                await check()
        except Exception as error:
            logger.warning(
                "dependency_unavailable",
                extra={"dependency": name, "error_type": type(error).__name__},
            )
            return name, "down"
        return name, "up"

    results = await asyncio.gather(
        *(run_check(name, check) for name, check in checks.items())
    )
    dependencies: dict[str, JsonValue] = {
        name: {"status": status} for name, status in results
    }
    return all(status == "up" for _, status in results), dependencies
