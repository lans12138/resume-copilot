"""Match-explanation layer (PORT-003).

Turns a completed candidate report into job-relevant, citation-backed
explanations produced by a model — and keeps the model strictly subordinate to
the deterministic rules that produced the verdict (BR-001, BR-002).

* ``schemas``   — the closed contract a model reply must satisfy.
* ``gateway``   — the model boundary, plus the deterministic offline fake.
* ``service``   — verification, degradation and persistence.
"""

from __future__ import annotations

__all__: list[str] = []
