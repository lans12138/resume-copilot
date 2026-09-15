# ADR-0001：保留自研节点引擎，不引入 LangGraph 运行时

- 状态：**已接受（Accepted）**
- 日期：2026-09-15
- 决策者：项目维护者
- 关联：FIN-009；IMP-018；编码实现计划 §19.2「Qwen 与 LangGraph 技术对齐」、§20.4
- 取代：`技术栈选型与架构决策.md` §8.1 中「采用 LangGraph 作为运行时」的原始表述（该文保留为**选型意图**记录，见下文「文档口径」）

---

## 1. 背景

`技术栈选型与架构决策.md` §8.1 在立项阶段选择 LangGraph 作为 Agent 编排运行时，理由是「可以显式建模节点、条件边、状态、重试和中断」「支持 Checkpoint、人工审批、恢复和历史回放」。后续的概要设计、详细设计、需求分析均以该选型为前提编写，`需求分析.md` AGT-008 更把「两类 Run 都必须使用 PostgreSQL Checkpointer」写成 Must 级需求。

但实际实现（IMP-018）走的是一条不同的路：`backend/app/agent/engine.py` 中的 `RunEngine` 是一个约 250 行的自研有序节点引擎。它并非 LangGraph，也从未依赖 LangGraph。这一偏差在 `编码实现计划.md` §19.2 已被登记为 `PARTIAL`：

> 真实 Qwen Chat/Embedding 适配器未实现；当前运行时不是计划中的 LangGraph，需要实现或正式记录设计偏差。

FIN-009 的第一个交付项正是要求对此形成正式 ADR：**采用真实 LangGraph，或正式确认保留当前引擎的偏差**。本 ADR 作出后者。

## 2. 事实核查（决策所依据的证据）

以下均为在本仓库中直接验证的事实，而非推测。

### 2.1 LangGraph 完全不存在于本项目

| 检查项 | 结果 |
| --- | --- |
| `pyproject.toml` 依赖声明 | 无 `langgraph` / `langchain` |
| `requirements.lock` / `requirements-dev.lock` | 无 `langgraph` / `langchain` |
| 源码导入（`backend` / `apps` / `tests`） | 零导入 |
| 代码中的文本出现 | 仅 5 处刻意说明「不是 LangGraph」的注释/文档字符串，以及 `model_gateway.py` 中一个技能关键词 |

`model_gateway.py:28` 把 `"langgraph"` 列入 `_SKILL_KEYWORDS`，那是 Fake 提取器用于识别简历中「懂 LangGraph 这项技能」的关键词表——与运行时选型无关，容易误读，特此澄清。

### 2.2 自研引擎的规模与边界

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `agent/engine.py` | 250 | `RunEngine` / `RunGraph` / `GraphNode` / `InterruptResult` |
| `agent/checkpoint.py` | 161 | `Checkpointer` 协议 + `InMemoryCheckpointer` + `SqlCheckpointer` |
| `agent/models.py` | 289 | `AgentRun` / `AgentCheckpoint` / `AgentEvent` / `CheckpointTuple` |
| `agent/repository.py` | 404 | 事件序列、状态迁移、Run 认领 |
| `agent/service.py` | 211 | Run 编排入口 |
| `agent/tasks.py` | 378 | Celery 任务与恢复校验 |
| `job_applications/graph.py` | 121 | ApplicationRun 图定义 |
| **合计** | **1814** | |

`engine.py` 的模块文档字符串已经诚实声明了这一偏差：

> `RunEngine` is intentionally small: it is *not* LangGraph. … the event/checkpoint contracts remain portable to a future LangGraph `StateGraph` implementation.

### 2.3 引擎的既有契约

引擎提供的行为与 LangGraph 的对应能力如下：

| LangGraph 能力 | 本项目对应实现 |
| --- | --- |
| `StateGraph` 节点与顺序边 | `RunGraph(nodes=[GraphNode...])` 有序列表 + `index_of()` |
| `interrupt_after` / 人工中断 | `interrupt_after` 主中断 + `interrupt_after_conditional` 条件中断（守卫 `state -> bool`） |
| `thread_id` 恢复键 | `AgentRun.thread_id`（全局唯一），`agent_checkpoints.thread_id` |
| Checkpoint 命名空间 | `checkpoint_ns`（引擎构造参数，默认 `""`） |
| `BaseCheckpointSaver` (`put`/`get`/`list`) | `Checkpointer` 协议，方法名刻意对齐 |
| 恢复后继续位置 | `checkpoint.metadata["next_node"]`，`"__END__"` 表示结束 |
| 状态注入（不改写检查点） | `resume(state_override=...)`，合并时 override 优先 |

`engine.py:199-206` 记录了一条关键的语义细节：主中断只在**首次执行**触发（恢复的 Run 不会重新进入主中断节点），而条件中断在**每次**守卫满足时触发——这正是「第二次审批闸门可以在恢复后的 Run 上再次暂停」的实现基础（对应详细设计 §10/§11）。

### 2.4 真正的工作量不在引擎行数，而在接线

这是本次决策的核心发现。若改用 LangGraph，需要吸收的**不是** 250 行引擎，而是围绕引擎的整套接线：

1. **每个节点的双事件写入**。`_run_from` 对每个节点前后各写一条 `AgentEvent`（`NODE_STARTED` / `NODE_COMPLETED`），并维护 `sequence` 单调递增；中断时再写 `STATUS_CHANGED`，恢复时写 `RUN_RESUMED`，结束写 `RUN_COMPLETED`。
2. **每个状态迁移的 `set_status` 调用**。`await self._repository.set_status(...)` 散落在中断（229 行）、恢复（159 行）与完成（249 行）三处。
3. **恢复校验散落三处**，不构成单一入口：
   - `agent/tasks.py:247-283`：`resume_token_missing`、`stale_resume_version`、`resume_not_approved` 三类拒绝，且校验 `approval.status is ApprovalStatus.EXECUTED`；
   - `job_applications/service.py:277-282`：无检查点与检查点过期时抛 `RuntimeError`；
   - `side_effects.py`：副作用幂等。

   这层「版本 + 已执行审批 + actor 一致」的三元校验是防重放的实质闸门，与 LangGraph 的检查点机制无关，迁移后仍需原样保留。

### 2.5 只有一张图真正使用引擎

- `job_applications/graph.py::build_application_graph()` 是**唯一**调用 `RunEngine` 的图（8 个节点，`interrupt_after="human_review"`，条件中断 `wait_schedule_approval`）。
- `match_run/service.py:152-225` 的 `execute_match_run` 是**手写的顺序过程**，直接调用 `self._node(...)`，从未触碰 `RunEngine`。

而 `需求分析.md:67` 与 AGT-008 要求的是「单 Agent、岗位级 MatchRun 与候选人级 ApplicationRun **两张** LangGraph 状态图」。因此计划书中的「迁移固定双图」在事实上等于：**迁移一张图 + 新写一张图**。MatchRun 图的任务是「迁移」，更是「构建」。

### 2.6 895 行不变量测试是不可谈判的约束

| 测试文件 | 行数 | 冻结的行为 |
| --- | --- | --- |
| `tests/unit/test_concurrency_recovery.py` | 365 | 重启、认领竞争、检查点恢复 |
| `tests/unit/test_approval.py` | 329 | 审批幂等、防重复执行副作用 |
| `tests/unit/test_agent_run_event.py` | 201 | 事件顺序、`sequence` 单调性 |
| **合计** | **895** | |

`test_concurrency_recovery.py:5` 的文档字符串已明确：这些契约「在真实 LangGraph + PG Checkpointer 替换（IMP-030）时也不能被静默破坏」。这说明偏差在实现之初就是**被记录在案的**，而非疏忽。

### 2.7 文档口径不一致

| 文档 | 立场 |
| --- | --- |
| `技术栈选型与架构决策.md:56` | 断言「Agent 编排 \| LangGraph + PostgreSQL Checkpointer」（表格行，读起来像既成事实） |
| `需求分析.md:269`（AGT-008） | Must 级需求直接假设 LangGraph 运行时 |
| `详细设计说明书.md:71`、`概要设计说明书.md:402/562` | 将 `thread_id` 描述为「LangGraph PostgreSQL Checkpointer 恢复键」 |
| `UML规划文档.md:666`、`技术栈选型与架构决策.md:240/314/427/740/919` | 规划/宣传性表述 |
| `项目可行性分析.md:34/51/92` | 技术可行性论证依赖 LangGraph |
| **`README.md:64`** | **唯一诚实的一条**：「基于 Checkpoint 的节点引擎（IMP-018）；真实 LangGraph + PG Checkpointer 为后续适配项」 |
| `编码实现计划.md:521` | 已登记为 `PARTIAL` |
| `AGENTS.md`、CI 配置 | 从未提及 LangGraph |

## 3. 决策

**保留自研 `RunEngine` + `SqlCheckpointer` 作为本项目在 FIN-009 范围内的正式 Agent 运行时。**不引入 LangGraph、langchain 或 LangGraph PostgreSQL Checkpointer 依赖。

同时，**正式承认这是一个有意识记录在案的设计偏差**，并按本 ADR §5 修正文档口径，使其与代码事实一致。

## 4. 备选方案与取舍

### 方案 A：引入真实 LangGraph StateGraph + PostgreSQL Checkpointer —— 否

优点：与立项选型一致；获得官方维护的调度、分支、子图、时间旅行与流式能力；文档无需改动。

否决理由，按权重排序：

1. **接线成本远大于引擎成本**。如 §2.4 所示，需要重写的核心是事件/状态/恢复校验这套 1800 行规模的接线，而非 250 行引擎。收益（更完整的图能力）与当前需求（两张固定顺序图 + 一个中断点）不匹配。
2. **895 行不变量测试需要重写而非复用**。这些测试直接针对 `RunEngine` 的语义断言，替换运行时意味着重订契约，风险高且收益不明确。
3. **依赖与锁文件风险**。本机缺少生成项目锁文件所需的 `pip-compile --no-index` 工具链，且索引为清华镜像；在此条件下改动 `requirements.lock` 的风险高于收益——这一点在 FIN-008 的技术选型（放弃 `openai` SDK 而复用既有 `httpx2`）中已被验证过。
4. **需要 Docker + PostgreSQL 才能验证**。本机 Docker 守护进程当前未运行，无法在本轮完成迁移的端到端验证。
5. **第二张图本来就不存在**。「采用 LangGraph」并不能省下 MatchRun 图的构建工作，只是把它从「画一张自研图」换成「画一张 LangGraph 图」，还额外叠加了接线重写。

### 方案 B：保留自研引擎 + 正式记录偏差 —— 采用

优点：

1. **零新依赖、零锁文件改动**，现有可复现基线（Python 3.12 容器、PostgreSQL + pgvector、Redis/Celery、Node 22）不受影响。
2. **895 行不变量测试全部保留**，行为契约不变。
3. **偏差本就被记录**（§2.6），本决策只是将其从「待办」升格为「已决定」。
4. **恢复契约已经成立并且可证明**：`metadata["next_node"]` + 版本 + 已执行审批的三元校验比单纯的检查点机制更严格（见 §6 验证）。

代价与缓解：

- **代价**：放弃了 LangGraph 的官方能力（条件边、子图、时间旅行、可视化）。缓解：当前两张图均为固定顺序 + 单/双中断点，无分支调度的真实需求；若将来出现多分支或循环，引擎的 `interrupt_after_conditional` 仍可扩展，且事件/检查点契约按设计保持「可迁移」（见 §5.3）。
- **代价**：文档与代码一度不一致。缓解：本 ADR 即为该偏差的单一事实来源，§5 同时修正关键表述。
- **代价**：需自行维护引擎。缓解：引擎 250 行、无外部依赖、有 895 行不变量测试覆盖。

## 5. 后果

### 5.1 项目层面的后果

- `编码实现计划.md` §19.2 该行从 `PARTIAL` 升级为 `DONE`，理由由「未实现」改为「已按 ADR-0001 正式确认为有意识偏差」。
- FIN-009 的后续交付项解释为：**保留当前引擎**，因此执行「同步全部设计与宣传材料」这一分支，力度为「精准修正断言『已是 LangGraph』的表述」。
- 需求 AGT-008 的口径调整为：**两类 Run 都必须使用持久化 Checkpointer 并在重启后按 `thread_id` 恢复**；具体实现为 `SqlCheckpointer`，不再强制特定第三方库。该条的需求实质（重启可恢复、检查点不存大文本）不变。

### 5.2 明确不改变的事项

- **业务表仍是事实源**。检查点只决定「图从哪里继续」，业务状态一律以 `job_applications` / `approvals` / `agent_runs` 等业务表为准（`checkpoint.py:9-12` 已将此写为设计规则）。
- **已执行 Approval 绝不因检查点重放再次执行副作用**。该保证来自 `run_id:attempt:action_type:ordinal` 业务幂等键与九层防重（`agent/tasks.py:262-272` 的 `resume_not_approved` 校验即其中之一），与运行时选型正交。
- **检查点不保存完整 PDF/DOCX 二进制或无界大文本**（AGT-009）。
- **事件契约、`thread_id` 语义、`checkpoint_ns` 语义**保持不变。

### 5.3 未来重新评估的触发条件

出现下列任一情况时，应重新开启本 ADR 并重新考虑方案 A：

1. 需要**原生多分支/循环调度**，使顺序列表 + 条件中断不再够用；
2. 需要**子图组合**或跨 Run 的图复用；
3. 需要**时间旅行/分叉重放**（从历史检查点分叉出平行执行）；
4. 团队规模扩大，需要 LangGraph 生态的可视化与调试工具；
5. LangGraph 的 `BaseCheckpointSaver` 契约发生变化，使现有 `Checkpointer` 协议对齐失效。

届时迁移的**建议路径**：保持 `Checkpointer` 协议与事件契约不变，仅替换 `engine.py` 内部的调度实现，并让 §2.6 的 895 行不变量测试作为迁移的验收门禁——因为「引擎可替换」正是原始设计的意图。

## 6. 验证

本决策的可行性由以下既有证据支撑（详见 FIN-009 的提交与计划书条目）：

- `tests/unit/test_concurrency_recovery.py`：证明 API/Worker 重启后可按 `thread_id` 从检查点恢复；证明认领竞争不会导致重复执行。
- `tests/unit/test_approval.py`：证明已执行 Approval 在重放时不会重复执行副作用。
- `tests/unit/test_agent_run_event.py`：证明节点事件顺序与 `sequence` 单调性。
- `tests/unit/test_fin009_adr_contract.py`（本次新增）：把本 ADR §2 的事实核查与 §5.2 的不改变事项固化为自动化断言，防止文档口径与代码事实再次漂移（例如「若无 ADR 依据，仓库不得出现 langgraph 依赖」，以及「恢复必须校验已执行 Approval」）。
- `scripts/project.ps1 verify` 全量门禁：pytest + ruff + mypy + tsc + 各探针脚本。

## 7. 文档口径

本 ADR 作出后：

- `技术栈选型与架构决策.md` §8.1 保留原始选型论证，但明确标注「**立项意图**；运行时最终实现见 ADR-0001」，并把 §8.1 的「排除其他选项 → 自写状态机：可控，但需要重复实现持久化、暂停和恢复」一句补充为「本项目最终选择此项，详见 ADR-0001」。
- 依赖句式的表格行与字段说明（`技术栈选型与架构决策.md:56`、`需求分析.md:269`、`详细设计说明书.md:71`、`概要设计说明书.md:402/562`）改为「持久化 Checkpointer（本仓库实现：`SqlCheckpointer`；见 ADR-0001）」。
- 「规划中 / 后续适配项」类的善意表述（`README.md:64`、`UML规划文档.md` 图注、`环境配置清单.md:527`）**保持不变**——它们描述的是目标状态，不是既成事实，无需改动。
