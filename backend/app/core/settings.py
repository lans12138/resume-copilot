"""Typed application settings shared by API, worker, and scheduler processes."""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    """Supported deployment environments for the MVP."""

    LOCAL = "local"
    TEST = "test"
    DEMO = "demo"


class LogFormat(StrEnum):
    """Supported log output formats."""

    JSON = "json"
    TEXT = "text"


class Settings(BaseSettings):
    """Versioned environment contract used by every backend process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: AppEnvironment = AppEnvironment.LOCAL
    app_name: str = "resume-copilot"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: LogFormat = LogFormat.JSON
    public_base_url: str = "http://localhost:8080"
    api_base_path: str = "/api/v1"
    cors_origins: str = "http://localhost:5173"
    request_id_header: str = "X-Request-ID"

    jwt_secret: SecretStr | None = None
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_ttl_minutes: int = Field(default=30, gt=0, le=1440)
    password_hash_scheme: Literal["argon2"] = "argon2"
    login_rate_limit: str = "10/minute"

    postgres_db: str = "resume_copilot"
    postgres_user: str = "resume_app"
    postgres_password: SecretStr | None = None
    database_url: SecretStr | None = None
    db_pool_size: int = Field(default=5, gt=0, le=100)
    db_pool_timeout: int = Field(default=30, gt=0, le=300)
    db_echo: bool = False

    redis_url: SecretStr | None = None
    celery_broker_url: SecretStr | None = None
    celery_result_backend: SecretStr | None = None
    celery_task_always_eager: bool = False
    celery_worker_concurrency: int = Field(default=2, gt=0, le=64)
    celery_task_time_limit: int = Field(default=300, gt=0)
    scheduler_timezone: str = "UTC"

    storage_backend: Literal["local-volume"] = "local-volume"
    storage_root: Path | None = None
    max_file_size_mb: int = Field(default=10, gt=0, le=100)
    max_batch_files: int = Field(default=20, gt=0, le=100)
    max_pdf_pages: int = Field(default=50, gt=0, le=1000)
    max_extracted_chars: int = Field(default=200_000, gt=0)
    parser_timeout_seconds: int = Field(default=60, gt=0, le=600)
    parser_version: str = "v1"

    model_base_url: str | None = None
    qwen_api_key: SecretStr | None = None
    chat_model: str = "qwen3.7-plus"
    model_timeout_seconds: int = Field(default=60, gt=0, le=600)
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_dimension: int = 1024
    embedding_batch_size: int = Field(default=16, gt=0, le=256)
    mock_model_mode: bool = True

    top_k: int = Field(default=10, gt=0, le=100)
    rrf_k: int = Field(default=60, gt=0)
    structured_weight: float = Field(default=1.0, ge=0)
    keyword_weight: float = Field(default=1.0, ge=0)
    vector_weight: float = Field(default=1.0, ge=0)
    hnsw_enabled: bool = False
    node_timeout_seconds: int = Field(default=120, gt=0, le=3600)
    max_transient_retries: int = Field(default=2, ge=0, le=10)
    approval_ttl_minutes: int = Field(default=60, gt=0)
    sse_heartbeat_seconds: int = Field(default=15, gt=0, le=60)
    sse_batch_size: int = Field(default=100, gt=0, le=1000)
    sse_retry_milliseconds: int = Field(default=1000, gt=0, le=60_000)

    langfuse_enabled: bool = False
    langfuse_host: str | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret: SecretStr | None = None
    prompt_version: str = "v1"
    rule_version: str = "v1"

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> Self:
        """Reject incomplete or internally inconsistent process configuration."""
        missing = [
            field_name
            for field_name in (
                "jwt_secret",
                "database_url",
                "redis_url",
                "celery_broker_url",
                "storage_root",
            )
            if getattr(self, field_name) is None
        ]
        if missing:
            raise ValueError(f"missing required configuration: {', '.join(missing)}")

        assert self.jwt_secret is not None
        if len(self.jwt_secret.get_secret_value()) < 48:
            raise ValueError("jwt_secret must contain at least 48 characters")

        assert self.database_url is not None
        if not self.database_url.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use postgresql+asyncpg")

        for field_name in ("redis_url", "celery_broker_url", "celery_result_backend"):
            secret_value = getattr(self, field_name)
            if secret_value is not None and not secret_value.get_secret_value().startswith("redis://"):
                raise ValueError(f"{field_name} must use redis://")

        if self.storage_root is not None and not self.storage_root.is_absolute():
            raise ValueError("storage_root must be an absolute path")

        if not self.api_base_path.startswith("/") or self.api_base_path.endswith("/"):
            raise ValueError("api_base_path must start with one slash and have no trailing slash")

        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", self.request_id_header) is None:
            raise ValueError("request_id_header must be a valid HTTP header name")

        if re.fullmatch(r"[1-9][0-9]*/(second|minute|hour)", self.login_rate_limit) is None:
            raise ValueError("login_rate_limit must use '<count>/<second|minute|hour>'")

        if self.structured_weight + self.keyword_weight + self.vector_weight <= 0:
            raise ValueError("at least one retrieval weight must be greater than zero")

        if not self.mock_model_mode and (self.model_base_url is None or self.qwen_api_key is None):
            raise ValueError(
                "model_base_url and qwen_api_key are required when mock mode is disabled"
            )

        if self.langfuse_enabled and (
            self.langfuse_host is None
            or self.langfuse_public_key is None
            or self.langfuse_secret is None
        ):
            raise ValueError(
                "langfuse_host, langfuse_public_key, and langfuse_secret are required when enabled"
            )

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process."""
    return Settings()
