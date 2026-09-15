"""Ranking snapshot assembly (IMP-015).

Combines the pure RRF fusion (``fusion.fuse``) with per-candidate hard-rule
evaluation (``hard_rules.evaluate_hard_rules``) into a frozen ``RankingSnapshot``.
The snapshot is what IMP-019 persists into ``match_run_candidates``; it carries
every channel rank/score, the exact ``RetrievalConfig``, and the stable order.

FAIL/UNKNOWN candidates are never removed here. Filtering is deferred to the
candidate-list UI (detailed design §8.4, §12.1) so evidence-scored reporting
still covers them.
"""

from __future__ import annotations

from backend.app.retrieval.fusion import fuse
from backend.app.retrieval.hard_rules import attach_hard_rules
from backend.app.retrieval.models import (
    JobQuery,
    RankingSnapshot,
    ReadyProfile,
    RecallBundle,
    RetrievalConfig,
)


def build_ranking_snapshot(
    bundle: RecallBundle,
    config: RetrievalConfig,
    job: JobQuery,
    profiles: list[ReadyProfile],
) -> RankingSnapshot:
    """Fuse a recall bundle and attach hard rules, returning a frozen snapshot."""
    fused = fuse(bundle, config)
    enriched = attach_hard_rules(fused, job, profiles)
    return RankingSnapshot(
        job_version_id=bundle.job_version_id,
        config=config,
        fused=enriched,
    )
