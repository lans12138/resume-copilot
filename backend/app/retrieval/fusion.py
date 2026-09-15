"""RRF fusion of the three recall channels (IMP-015).

``fuse`` is a pure function over a ``RecallBundle`` and a ``RetrievalConfig``:
for fixed inputs it always produces the same ordering, because the only free
inputs are the per-channel ranks (already deterministic from IMP-014) and the
config weights. Unhit channels contribute nothing. The fused list is sorted by
``rrf_score`` descending, then ``profile_id`` ascending, so ties break
stably and reproducibly — the property the Golden Dataset depends on.
"""

from __future__ import annotations

from uuid import UUID

from backend.app.retrieval.models import (
    ChannelName,
    FusedCandidate,
    RecallBundle,
    RecallHit,
    RetrievalConfig,
)


def _weight_for(channel: ChannelName, config: RetrievalConfig) -> float:
    if channel is ChannelName.STRUCTURED:
        return config.structured_weight
    if channel is ChannelName.KEYWORD:
        return config.keyword_weight
    return config.vector_weight


def fuse(bundle: RecallBundle, config: RetrievalConfig) -> list[FusedCandidate]:
    """Fuse the three channels into a Top-K ``FusedCandidate`` list.

    RRF score for a profile is ``Σ channel_weight / (rrf_k + rank_channel)``
    over every channel that ranked it. Channels in ``config.channels`` that the
    bundle left empty simply contribute nothing for any profile.
    """
    channel_hits: dict[ChannelName, list[RecallHit]] = {
        ChannelName.STRUCTURED: bundle.structured,
        ChannelName.KEYWORD: bundle.keyword,
        ChannelName.VECTOR: bundle.vector,
    }

    scores: dict[UUID, float] = {}
    ranks: dict[UUID, dict[ChannelName, tuple[int, float]]] = {}

    for channel, hits in channel_hits.items():
        weight = _weight_for(channel, config)
        for hit in hits:
            scores[hit.profile_id] = (
                scores.get(hit.profile_id, 0.0) + weight / (config.rrf_k + hit.rank)
            )
            ranks.setdefault(hit.profile_id, {})[channel] = (hit.rank, hit.score)

    ordered = sorted(scores, key=lambda pid: (-scores[pid], pid))
    top = ordered[: config.top_k]

    fused: list[FusedCandidate] = []
    for order, profile_id in enumerate(top, start=1):
        channel_ranks = ranks.get(profile_id, {})
        structured = channel_ranks.get(ChannelName.STRUCTURED)
        keyword = channel_ranks.get(ChannelName.KEYWORD)
        vector = channel_ranks.get(ChannelName.VECTOR)
        fused.append(
            FusedCandidate(
                candidate_profile_id=profile_id,
                snapshot_order=order,
                rrf_score=scores[profile_id],
                structured_rank=structured[0] if structured else None,
                structured_score=structured[1] if structured else None,
                keyword_rank=keyword[0] if keyword else None,
                keyword_score=keyword[1] if keyword else None,
                vector_rank=vector[0] if vector else None,
                vector_score=vector[1] if vector else None,
            )
        )
    return fused
