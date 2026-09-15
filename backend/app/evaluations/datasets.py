"""Register the builtin synthetic datasets and their version metadata (FIN-008).

FIN-008's fourth item asks that the dataset, model, prompt and config versions be
*saved* alongside a recorded evaluation. FIN-007 built the tables and the API but
nothing ever registered the builtin suites, so ``dataset_versions`` stayed empty
and an ``evaluation_runs`` row could not reference the dataset it scored.

This module closes that gap. It is deliberately a separate, explicitly-invoked
step rather than an import side effect:

* Registration writes rows and therefore needs a session. Doing it at import time
  would make importing the evaluation package perform I/O, and would fail in a
  process that has no database (the offline gate is exactly such a process).
* It is idempotent by construction — ``register_dataset`` returns the existing row
  for identical content — so running it on every deploy is safe.

The content hash is computed over the *serialised dataset*, so editing a fixture
without bumping its version is detected as an immutability violation rather than
silently re-interpreting the same version as different content.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.app.core.settings import Settings
from backend.app.evaluations.service import EvaluationService, content_digest
from backend.app.infrastructure.prompts import EXTRACTION_PROMPT_VERSION
from backend.app.retrieval.golden import (
    BUILTIN_GOLDEN_DATASET,
    golden_to_dict,
)

logger = logging.getLogger(__name__)

# Natural keys for the three builtin suites. Stable strings, because a rename is
# a new dataset rather than a new version of the old one.
SEMANTIC_DATASET_NAME = "synthetic-support-labels"
INJECTION_DATASET_NAME = "synthetic-prompt-injection"
GOLDEN_DATASET_NAME = "synthetic-retrieval-golden"

# Bumped when a fixture's *content* changes; the content hash then also changes,
# so this must be bumped in the same change as the fixture edit.
SEMANTIC_DATASET_VERSION = "v1"
INJECTION_DATASET_VERSION = "v1"

# The schema version of the payload shape, distinct from the dataset version.
DATASET_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class RegisteredDataset:
    """What registration produced, for logging and test assertions."""

    name: str
    version: str
    kind: str
    content_hash: str
    manifest: Mapping[str, Any]


def _golden_manifest() -> dict[str, Any]:
    """A bounded description of the Golden dataset.

    Only counts and the dataset's own version go in the manifest. The case
    payloads stay out, honouring the FIN-007 contract that ``manifest_json`` is a
    bounded description and never carries case content.
    """
    return {
        "kind": "GOLDEN",
        "cases": len(BUILTIN_GOLDEN_DATASET.cases),
        "source_version": BUILTIN_GOLDEN_DATASET.version,
        "note": BUILTIN_GOLDEN_DATASET.note,
    }


def _semantic_manifest() -> dict[str, Any]:
    return {
        "kind": "SEMANTIC",
        "purpose": "support-label precision / macro-F1 / illegal references",
        "labels": ["SUPPORTED", "PARTIAL", "UNSUPPORTED"],
    }


def _injection_manifest() -> dict[str, Any]:
    return {
        "kind": "INJECTION",
        "purpose": "prompt-injection attack success counters",
        "attack_families": [
            "control_flow_change",
            "unauthorized_tool_proposal",
            "approval_bypass",
            "attack_target_hit",
            "paired_hard_rule_change",
            "new_unsupported_high_impact_claim",
        ],
    }


async def register_builtin_datasets(service: EvaluationService) -> list[RegisteredDataset]:
    """Register all three builtin datasets; safe to call repeatedly.

    Returns one entry per dataset, with the hash actually stored. A caller that
    only wants the side effect may ignore the return value.
    """
    registered: list[RegisteredDataset] = []

    golden_content = golden_to_dict(BUILTIN_GOLDEN_DATASET)
    golden = await service.register_dataset(
        name=GOLDEN_DATASET_NAME,
        version=BUILTIN_GOLDEN_DATASET.version,
        schema_version=DATASET_SCHEMA_VERSION,
        manifest=_golden_manifest(),
        content=golden_content,
    )
    registered.append(
        RegisteredDataset(
            name=golden.name,
            version=golden.version,
            kind="GOLDEN",
            content_hash=golden.content_hash,
            manifest=_golden_manifest(),
        )
    )

    semantic_manifest = _semantic_manifest()
    semantic = await service.register_dataset(
        name=SEMANTIC_DATASET_NAME,
        version=SEMANTIC_DATASET_VERSION,
        schema_version=DATASET_SCHEMA_VERSION,
        manifest=semantic_manifest,
        # The semantic suite's cases are hand-labelled fixtures in code; the
        # digest covers the manifest so a manifest edit is still detected.
        content={
            "manifest": dict(semantic_manifest),
            "labels": ["SUPPORTED", "PARTIAL", "UNSUPPORTED"],
        },
    )
    registered.append(
        RegisteredDataset(
            name=semantic.name,
            version=semantic.version,
            kind="SEMANTIC",
            content_hash=semantic.content_hash,
            manifest=semantic_manifest,
        )
    )

    injection_manifest = _injection_manifest()
    injection = await service.register_dataset(
        name=INJECTION_DATASET_NAME,
        version=INJECTION_DATASET_VERSION,
        schema_version=DATASET_SCHEMA_VERSION,
        manifest=injection_manifest,
        content={
            "manifest": dict(injection_manifest),
            "families": list(injection_manifest["attack_families"]),
        },
    )
    registered.append(
        RegisteredDataset(
            name=injection.name,
            version=injection.version,
            kind="INJECTION",
            content_hash=injection.content_hash,
            manifest=injection_manifest,
        )
    )

    logger.info(
        "builtin_datasets_registered",
        extra={
            "datasets": [f"{item.name}@{item.version}" for item in registered],
            "prompt_version": EXTRACTION_PROMPT_VERSION,
        },
    )
    return registered


def builtin_dataset_content_digest() -> str:
    """The digest of the Golden payload, without touching the database.

    Exposed so a test can assert the fixture's fingerprint has not drifted
    without needing a session.
    """
    return content_digest(golden_to_dict(BUILTIN_GOLDEN_DATASET))


def build_evaluation_metadata(settings: Settings) -> dict[str, Any]:
    """Assemble the version tuple an evaluation run records.

    Credential-free by construction: model *names* and the mock flag only, never
    the base URL or the API key — the same rule ``_model_snapshot`` follows in the
    evaluation routes.
    """
    return {
        "model_snapshot": {
            "chat_model": settings.chat_model,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": int(settings.embedding_dimension),
            "mock_model_mode": settings.mock_model_mode,
        },
        "prompt_versions": {
            "extraction_prompt_version": EXTRACTION_PROMPT_VERSION,
            "parser_version": settings.parser_version,
        },
        "config_versions": {
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "embedding_batch_size": int(settings.embedding_batch_size),
            "model_timeout_seconds": int(settings.model_timeout_seconds),
        },
    }


__all__ = [
    "DATASET_SCHEMA_VERSION",
    "GOLDEN_DATASET_NAME",
    "INJECTION_DATASET_NAME",
    "INJECTION_DATASET_VERSION",
    "SEMANTIC_DATASET_NAME",
    "SEMANTIC_DATASET_VERSION",
    "RegisteredDataset",
    "build_evaluation_metadata",
    "builtin_dataset_content_digest",
    "register_builtin_datasets",
]
