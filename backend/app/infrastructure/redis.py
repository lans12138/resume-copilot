"""Async Redis client construction and readiness probe."""

from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis

from backend.app.core.settings import Settings


def build_redis_client(settings: Settings) -> Redis:
    """Build a decoded async client without connecting eagerly."""
    assert settings.redis_url is not None
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


async def check_redis(client: Redis) -> None:
    """Verify that the configured Redis responds to PING."""
    response = await cast(Awaitable[bool], client.ping())
    if response is not True:
        raise RuntimeError("redis health query returned an unexpected value")
