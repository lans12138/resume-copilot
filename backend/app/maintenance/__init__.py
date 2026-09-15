"""Maintenance coordination tasks (IMP-023): Approval timeout sweep, etc.

The sweeper is the scheduled counterpart to the human decision path. Where
``ApprovalService.decide`` rejects an already-expired approval (§11.5 step 3),
the sweeper proactively promotes a ``PENDING`` approval whose ``expires_at`` has
passed into ``EXPIRED/TIMEOUT`` and fails its run as *retryable* so a new attempt
can reclaim the slot (§11.8). The same lock order and the same "still PENDING"
re-check make the two paths race-safe: whichever runs first wins, the loser is a
no-op.
"""
