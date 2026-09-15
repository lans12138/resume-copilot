---
name: resume-approval-sideeffect
description: >
  This skill should be used when adding a new ApprovalActionType with a persisted
  side effect to the resume-copilot backend (ApplicationRun double-approval flow,
  IMP-024+). It captures the exact wiring: the two-stage interrupt graph, the
  ApplicationSideEffectService "execute exactly once" contract, and the
  EXECUTION_FAILED re-anchor retry that the JobApplication.version optimistic lock
  otherwise blocks. Load it before implementing IMP-025~030 (events / evaluations /
  observability / any new gated mutation).
agent_created: true
---

# Resume Approval Side-Effect Wiring

## Overview

resume-copilot gates every ApplicationRun mutation behind human approval (detailed
design §10/§11, G5). A new gated action is NOT a direct DB write — it is a two-stage
approval driven by a fixed graph, applied by `ApplicationSideEffectService` exactly
once, and recoverable on a transient backend failure. This skill records the wiring
so a new `ApprovalActionType` lands in all the right places without re-discovering
the contract.

## When to use

Trigger this skill whenever the task introduces or modifies:
- A new ApprovalActionType (e.g. a future `CREATE_EVALUATION`, `PUBLISH_EVENT`).
- The double-approval graph in `backend/app/job_applications/graph.py`.
- `ApplicationSideEffectService` (`backend/app/job_applications/side_effects.py`).
- `ApplicationRunService.decide_approval` two-stage orchestration.
- Anything touching `JobApplication.version`, `ApplicationStatusHistory`, or the
  active-run slot (`claim_active_run` / `clear_active_run`).

## Core contract (do not violate)

1. **Graph nodes are pure** `(state) -> state`. No DB write inside a graph node.
   Every side effect goes through `ApplicationSideEffectService` + an execution
   service, called from `decide_approval` *before* `resume_run`.
2. **Two interrupts**: `human_review` (always) then `wait_schedule_approval`
   (conditional, only when `needs_interview` is true). The engine supports
   conditional interrupts via `interrupt_after_conditional={"node": guard}` and the
   guard fires on BOTH `execute` and `resume` paths (`RunEngine` change in IMP-024).
   Non-SHORTLISTED targets (ON_HOLD / REJECTED) skip the second interrupt and go
   straight to `COMPLETED`.
3. **Exactly once**: `Approval` state machine `PENDING -> APPROVED/EDITED ->
   EXECUTED` (or `EXECUTION_FAILED`). An already-`EXECUTED` approval, or an
   interview already bound to the approval, returns a no-op. `idempotency_key`
   travels into the schedule/backend so a duplicate request returns the same
   external id.
4. **Optimistic lock**: `JobApplication.version` is bumped by `claim_active_run`
   (1->2), `clear_active_run` (+1), `execute_update_status` (+1),
   `execute_create_schedule` (+1). The CAS in `ApplicationSideEffectService`
   compares `approval.expected_application_version` against `application.version`.
   `expected_application_version` is frozen at `create_approval` time to the
   application version at that moment.

## Reusable procedure for a new action type

1. Add the `ApprovalActionType` enum member.
2. Add a graph node that *sets a state marker only* (no DB write) and, if it needs
   a human gate, declare it in `interrupt_after_conditional` with a guard reading
   state.
3. Add an `execute_<action>` method to `ApplicationSideEffectService`:
   - Reject wrong `action_type` with `APPROVAL_WRONG_ACTION`/409.
   - Return no-op if `approval.status is EXECUTED` (idempotent backstop).
   - Reject if `approval.status not in _EXECUTABLE_STATUSES` (`APPROVAL_NOT_EXECUTABLE`).
   - Resolve the `JobApplication` via `arun_repo.get_application_run(
     approval.application_run_id).application_id` — **not** `approval.application_run_id`
     directly (that is the run id, not the application id).
   - **Re-anchor for retry**: if `approval.status is EXECUTION_FAILED`, set
     `approval.expected_application_version = application.version` *before* the CAS,
     so a controlled retry after a `clear_active_run` (which bumped version) is not
     rejected with `VERSION_CONFLICT`.
   - CAS: raise `VERSION_CONFLICT`/409 when versions differ.
   - Apply the mutation, bump `application.version += 1`, append
     `ApplicationStatusHistory`, then `approval_service.mark_executed`.
   - On a transient backend failure, raise a typed error; the caller marks
     `EXECUTION_FAILED` + `run_service.mark_failed(retryable=True)` + clears the
     slot and lets the same idempotency key retry.
4. Wire `decide_approval` in `job_applications/service.py`: after `execute_<action>`,
   `resume_run`; if the run is `COMPLETED`, clear the slot; if the executor raised
   `AppError` (retryable), clear the slot and return the run as-is.

## Pitfalls (caught in IMP-024)

- **Version assertion drift**: after `claim_active_run` the application is already
  at version 2, so a single status transition lands at version 3, not 2. Tests that
  assert post-decision version must account for the claim bump.
- **Retry `VERSION_CONFLICT`**: the failure path's `clear_active_run` bumps version
  (e.g. 3->4) while `approval.expected_application_version` stays at 3. The
  EXECUTION_FAILED re-anchor above is the fix — never skip the CAS globally.
- **Wrong id resolution**: `approval.application_run_id == AgentRun.id`, not the
  `JobApplication.id`. Resolve through `ApplicationRun`.

## Verification

Run before committing (local isolated venv at
`C:/Users/lanqi/.workbuddy/binaries/python/envs/default/Scripts/python.exe`):

```
$PY -m ruff check backend/app/
$PY -m mypy backend/app/
$PY -m pytest tests/unit -q \
  --ignore=tests/unit/test_settings.py --ignore=tests/unit/test_auth.py \
  --ignore=tests/unit/test_api_smoke.py --ignore=tests/unit/test_request_contract.py
```

The 4 ignore targets are pre-existing environment-broken files, not IMP regressions.
Add a `tests/unit/test_side_effects.py` case per new action (executed-once,
non-gated-skip, wrong-action rejection, failure-then-retry).
