# 案例：恢复之后没有重复写入

PORT-006 要求「至少一个恢复后没有重复写入的案例」。这份文档给出的是**一个可以自己跑出来的案例**，不是一段说明：涉及哪条不变量、由哪几层守着、断言落在哪个测试、以及怎么复现。

## 场景

一次 ApplicationRun 已经执行完第一次副作用（申请状态 → `SHORTLISTED`，写库成功），此时：

- Worker 崩溃或被杀，Celery 按至少一次语义**重新投递**了这条任务；
- 或者 checkpoint 没有推进，引擎从上一个断点**重放**了同一个节点。

两种情况的共同点是：**写操作被送到了第二次**。要证明的不是「它不会被送第二次」，而是「送第二次也不会写第二次」。

## 结论与证据

| 断言 | 位置 | 值 |
|---|---|---|
| 第一次执行确实生效了 | `tests/unit/test_side_effects.py::test_update_status_executed_once_and_idempotent` | 申请状态 `SHORTLISTED`，`version == 3` |
| 重放同一个已 `EXECUTED` 的审批**没有**产生第二次状态迁移 | 同上；`tests/unit/test_concurrency_recovery.py::test_replay_after_committed_write_is_idempotent` | `execute_update_status(...).executed is False`，审批仍为 `EXECUTED` |
| 申请版本号**没有变化**（这是最关键的一条） | 同上 | 重放前后均为 `version == 3` |
| 取消之后，后续节点与副作用写入被拒绝 | `tests/unit/test_concurrency_recovery.py::test_cancel_rejects_later_node_writes` | 申请保持 `CREATED`，`active_application_run_id is None`；取消后的审批决定抛 `APPROVAL_RUN_NOT_ACTIVE` / `APPROVAL_ALREADY_DECIDED` |
| 崩溃的请求不会永久占住幂等键 | `tests/unit/test_idempotency.py::test_api_restart_takes_over_stale_in_progress` | 超过 TTL 后新请求接管（`kind == "new"`），之后的相同请求回放已存响应（`kind == "replay"`） |
| 并发双击只产生一次副作用 | `tests/unit/test_idempotency.py::test_concurrent_double_click_single_side_effect`（PostgreSQL 版本：`tests/integration/test_idempotency_postgres.py::test_sql_concurrent_double_click_single_side_effect`） | 副作用执行计数为 1 |

**为什么用「版本号没变」当证据**：`JobApplication.version` 是乐观锁，每次真实状态迁移都会递增。如果重放写进去了第二次，版本号必然变大。这比「没有报错」强得多——「没有报错」也可能是静默失败。

## 由哪几层守着

三层是**互相独立**的，任何一层单独失效都不会导致重复写入：

1. **Approval 状态机**：`PENDING → 终态`只允许流转一次。已经 `EXECUTED` 的审批不会回到可决策状态，重复决定返回 409 `APPROVAL_ALREADY_DECIDED`。
2. **SideEffect 服务**：拿到一个已 `EXECUTED` 的审批时，直接复用已记录的结果并返回 `executed=False`，不再触碰业务表。
3. **乐观锁 CAS**：真正写库时带 `expected_application_version`。即使前两层都被绕过，一个基于陈旧版本号的写入也会被拒绝，而不是覆盖。

**幂等键**是第四层，管的是**入口**：同一 `operation + actor + key` 的相同请求返回原结果，不同请求返回 409 `IDEMPOTENCY_KEY_REUSED`。它防的是重复提交，不是重复投递——这两件事经常被混为一谈。

## 不要把它说成「Celery 只投递一次」

这是最容易讲错的地方，说错了反而暴露理解偏差：

- Celery 的投递语义**仍然是至少一次**。系统没有、也没打算阻止任务被重投。
- 被保证的是**业务可观察效果**等效于恰好一次：底层可能执行两次，但第二次是 no-op。
- 因此正确的说法是「业务效果等效的 exactly-once」，而不是「exactly-once 投递」。`/api/v1/metrics` 里的 401/403/409 计数可以现场佐证重复决定确实被拒。

## 复现

```bash
# 三条核心断言（恢复重放、取消拒绝写入、副作用恰好一次）
python -m pytest -q tests/unit/test_concurrency_recovery.py tests/unit/test_side_effects.py -v

# 幂等入口层（并发双击、崩溃接管、键复用契约）
python -m pytest -q tests/unit/test_idempotency.py -v

# PostgreSQL 上的同一组契约（需要 DATABASE_URL，无库时按设计跳过）
python -m pytest -q tests/integration/test_idempotency_postgres.py -v
```

本次实测（2026-09-19）：上面前两条命令合并运行 **15 passed**。第三条因本机无 `DATABASE_URL` 按设计跳过。

不带 `DATABASE_URL` 时集成用例会跳过——**跳过不等于通过**，它由探针栈在 `pwsh scripts/project.ps1 verify` 里执行。本机跑不了完整门禁时，请把这条限制和上表一起读。

## 现场怎么演示

10 分钟现场版的幕 5 用这条链路：跑一次审批到「已执行」→ 重启 `api worker` → 刷新页面确认 Run 与审批记录完好 → 在终端跑上面的第一条命令，指出测试名与 `passed`。录制版因为剪掉实时等待，改为展示恢复后的事实加这条命令的输出。
