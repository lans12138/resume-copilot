"""Controllable runtime resources for readiness contract tests."""

from __future__ import annotations

from typing import cast

from backend.app.infrastructure.runtime import RuntimeResources


class FakeRuntimeResources:
    def __init__(self, *unavailable: str) -> None:
        self.unavailable = frozenset(unavailable)
        self.closed = False

    async def _check(self, name: str) -> None:
        if name in self.unavailable:
            raise RuntimeError(f"{name} internal diagnostic")

    async def check_postgres(self) -> None:
        await self._check("postgres")

    async def check_redis(self) -> None:
        await self._check("redis")

    async def check_storage(self) -> None:
        await self._check("storage")

    async def close(self) -> None:
        self.closed = True


def make_fake_resources(*unavailable: str) -> RuntimeResources:
    return cast(RuntimeResources, FakeRuntimeResources(*unavailable))
