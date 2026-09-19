# resume-copilot

面向 HR / 招聘主管的**辅助招聘 Copilot**——不是自动招聘，而是一条**可验证的辅助决策链**：

> 整理简历 → 可评测召回 → 带证据的解释性匹配 → 批量分析与单人审批隔离 → 副作用前暂停审批 → 故障可恢复。

项目是六周单人开发的**作品集级 MVP**，目标岗位 AI Agent 开发。设计围绕三条不可妥协的硬约束展开。

面向求职作品集的下一轮改进见 [后续开发计划](./后续开发计划.md)，包含 2026-09-18 的代码现状复核、优先级、验收标准和发布安排。

## 三条硬约束（设计灵魂）

1. **证据链一等公民**：每个确定性结论必须引用 `EvidenceChunk`（精确摘录 + `SUPPORTED` / `PARTIAL` / `INSUFFICIENT`）。证据不足只能输出「不足以判断」。
2. **人工审批 + 幂等**：副作用执行前必须 `interrupt` 等人审批；未审批副作用执行次数 = 0；同一审批只决定一次。
3. **可恢复性**：Checkpoint 恢复、协作式取消、Approval 过期重试。

> 文档 / 岗位描述一律按**不可信数据**处理：它们不能改变控制流、权限或工具参数（由 IMP-027 Prompt Injection 配对评测门禁守护）。

## 架构

```mermaid
flowchart TB
    BROWSER["浏览器"]

    subgraph WEB["web 服务 · 唯一公开入口 · 127.0.0.1:8080"]
        NGX["Nginx 1.29.3-alpine<br/>静态 SPA + /api 反向代理<br/>两条 SSE 路由关缓冲"]
    end

    subgraph FRONTEND["前端 (React 19 + TS + Vite)"]
        UI["RunTimeline / RankingTable / ClaimEvidencePanel<br/>Approval / Interview / Evaluations"]
    end

    subgraph API["API (FastAPI + Pydantic v2)"]
        RG["MatchRun 图<br/>(岗位级批量分析)"]
        AG["ApplicationRun 图<br/>(单候选人 · 双审批)"]
        SSE["SSE 持续授权流<br/>(断线恢复 / 在线撤权)"]
        APP["Approval 状态机<br/>(幂等键 / 决定一次)"]
        SE["SideEffect 服务<br/>(CAS + 幂等执行一次)"]
    end

    subgraph STATE["持久化与任务"]
        PG[("PostgreSQL 17<br/>+ pgvector(1024)")]
        EV["AgentEvent 审计流"]
        REDIS[("Redis 7")]
        CELERY["Celery Worker / Beat<br/>解析 / 重试 / 过期扫描 / 孤儿清理"]
    end

    BROWSER --> NGX
    NGX --> FRONTEND
    FRONTEND -- "同源 /api/v1 REST + SSE" --> NGX
    NGX -- "Bearer 头透传" --> API
    RG --> PG
    AG --> APP
    APP -- "approve" --> SE
    SE --> PG
    AG --> EV
    SSE --> REDIS
    CELERY --> PG
    RG -. "不创建 Approval / 不进 WAITING_APPROVAL" .- AG
```

**单 Agent 双运行图**（隔离靠 `JobApplication` 活动 Run 排他槽 + Approval 幂等键）：

- **MatchRun**：岗位级批量分析，跑完即止——不创建 Approval、不改 `JobApplication` 状态。
- **ApplicationRun**：固定 8 节点**双审批图**（`human_review` 主中断 + `wait_schedule_approval` 条件中断）。副作用节点只埋 typed state，真正写表由 SideEffect 服务经 Approval 幂等键保证**只执行一次**。

**Nginx 是唯一公开入口**：前端只发同源请求（`/api/v1/...`），由代理决定上游，因此同一份静态产物可部署到任意环境。API 与数据库端口即使在本地也只绑定 `127.0.0.1`。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 19 + TypeScript 5.9 + Vite 7 + React Router 7 + TanStack Query 5 + Zustand 5（组件与样式自研，不引入 UI 组件库） |
| 后端 | Python 3.12.14 + FastAPI 0.141 + Pydantic 2.13 + SQLAlchemy 2 / Alembic |
| 数据库 | PostgreSQL 17 + pgvector（1024 维向量，镜像 digest 固定） |
| 任务 | Redis 7.4 + Celery 5.6（Worker + Beat） |
| Agent | 基于 Checkpoint 的节点引擎（IMP-018）；采用自研 `RunEngine` 而非 LangGraph，理由见 [ADR-0001](./docs/adr/0001-agent-runtime-custom-engine-over-langgraph.md) |
| 模型 | `qwen3.7-plus` + `qwen3.7-text-embedding`（OpenAI 兼容），默认 `MOCK_MODEL_MODE=true` |
| 入口 | Nginx 1.29.3-alpine（由 `web` 服务的运行时阶段提供，无独立 nginx 服务） |
| 编排 | Docker Compose + GitHub Actions（普通 CI 全程 FakeModel，无外部凭据） |

## 快速开始

前置只需要 **Docker Desktop（含 Compose v2）**。Node 22 不是演示必需的——前端在镜像内由固定 Node 版本构建。

```bash
# Windows 日常启动：无需安装 PowerShell 7，也不修改系统执行策略
.\start.cmd
#   入口：http://localhost:8080
```

`start.cmd` 只对这一次进程绕过 PowerShell 执行策略，然后调用同目录的 `start.ps1`；不会修改机器的永久执行策略。启动器首次运行会自动生成 `.env`，默认保留已有容器卷和业务数据，重复运行是安全的。为避开本机已有开发服务，API、PostgreSQL 和 Redis 的宿主调试端口默认自动分配，公开入口仍固定为 `8080`；需要固定调试端口时可传 `-ApiPort 8000 -PostgresPort 5433 -RedisPort 6380`。其他可选参数可直接透传，例如 `.\start.cmd -OpenBrowser`、`.\start.cmd -ResetDemo` 或 `.\start.cmd -Fresh`；`-Fresh` 与 `-ResetDemo` 不能同时使用。

底层的 `scripts/start_stack.ps1` 是全新环境验证脚本：它拒绝复用残留资源、把 seed 连跑两次验证幂等，并默认在验证后连卷拆除；需要直接使用时加 `-KeepRunning` 才会保留栈。

手工等价步骤：

```bash
docker compose build api web
docker compose up -d --wait          # migrate 在默认 profile，api 以 service_completed_successfully 等它
docker compose --profile tools run --rm seed
# 浏览器打开 http://localhost:8080
```

## 演示入口与账号

| 入口 | 地址 | 说明 |
|---|---|---|
| 公开入口 | `http://localhost:8080` | Nginx 提供静态 SPA 并反代 `/api/`；`/healthz` 由 Nginx 自答 |
| API 调试 | `http://127.0.0.1:8000` | 直接运行 Compose 时的默认值；`start.cmd` 自动选择空闲端口，或用 `-ApiPort` 固定 |
| PostgreSQL 调试 | `127.0.0.1:5433` | 直接运行 Compose 时的默认值；`start.cmd` 自动选择空闲端口，或用 `-PostgresPort` 固定 |
| Redis 调试 | `127.0.0.1:6380` | 直接运行 Compose 时的默认值；`start.cmd` 自动选择空闲端口，或用 `-RedisPort` 固定 |

演示账号由 `scripts/seed_demo_data.py` 写入，是**合成数据的固定凭据**（只对本地演示库有效，不含任何真实账号）：

- 用户名：`hr.demo`
- 密码：`demo-password-123`

## 演示数据流

1. **岗位**：种子已创建一个 ACTIVE 岗位「`[DEMO] 高级后端工程师（Go / Python）`」。
2. **批量匹配（MatchRun）**：在岗位页发起 MatchRun，对 5 名候选人做岗位级分析（不审批、无副作用）。
3. **单人审批（ApplicationRun）**：对单候选人发起 ApplicationRun：
   - 在 `human_review` 中断处停留，**必须 HR 审批**才能继续；
   - 若进入排期阶段，在 `wait_schedule_approval` 二次中断，**再次审批**才写面试；
   - 所有副作用经 Approval 幂等键保证**只执行一次**（`/api/v1/metrics` 记录 401/403/409）。
4. **面试与完成**：审批通过后生成 Interview，Run 进入 `COMPLETED`，活动槽释放。

## 演示 / 重置 / 诊断命令

| 目的 | 命令 | 说明 |
|---|---|---|
| 全量门禁 | `pwsh scripts/project.ps1 verify` | 与 CI 完全同一入口（CI 也只是调用它），串起静态探针、Ruff、mypy strict、pytest、离线评测门禁、前端 typecheck/vitest/build、Playwright 与两条活栈探针。需要 `pwsh`。 |
| 一键起停 | `pwsh scripts/start_stack.ps1 [-KeepRunning]` | 全新 clone/Volume 路径；结束默认清理。 |
| 起栈（保留数据） | `docker compose up -d --wait` | 迁移随默认 profile 自动执行。 |
| 灌演示数据 | `pwsh scripts/project.ps1 seed` | 幂等；第二次运行是 no-op。 |
| 重置演示数据 | `pwsh scripts/project.ps1 reset-demo` | 等价 `seed -- --reset`：按演示标记清理后重新灌入。 |
| 入口存活 | `curl.exe -i http://localhost:8080/healthz` | Nginx 自答 `ok`；API 挂掉时它仍返回 200，这是刻意设计。 |
| 依赖就绪 | `curl.exe http://localhost:8080/api/v1/health/ready` | 经代理打到 API，覆盖 PostgreSQL / Redis / Storage。 |
| 指标 | `curl.exe http://localhost:8080/api/v1/metrics` | 进程内指标（含 401/403/409 计数）。 |
| 真实模型诊断 | `python -m backend.app.infrastructure.model_diagnose` | 退出码 0=可达 / 1=永久失败 / 2=临时失败；端点做归一化，**绝不打印凭据**。 |
| 离线评测门禁 | `python -m backend.app.evaluations.gate` | 退出码 0=通过 / 1=阈值未达标 / 2=飞架故障；刻意不读 `Settings`，可在裸 CI 步骤独立运行。 |
| 看日志 | `docker compose logs -f api worker scheduler` | Worker / Scheduler 不设 Docker healthcheck，就绪性由探针日志断言。 |
| 彻底清理 | `docker compose --profile tools down --volumes --remove-orphans` | **必须带 `--profile tools`**：`storage-init` 在该 profile 内，漏掉它就删不掉它持有的 `resume_storage` 卷。 |
| Docker 自愈 | `pwsh scripts/project.ps1 docker-recover` | 只把冲突目录/设置文件改名备份，不删镜像、容器数据或 VHDX。 |

## 开发与验证

```bash
# 后端质量门禁（容器内，本机无需 Python 环境）
pwsh scripts/project.ps1 backend-test        # = docker run --rm resume-copilot-backend-development:local pytest -q
pwsh scripts/project.ps1 backend-lint        # ruff
pwsh scripts/project.ps1 backend-typecheck   # mypy strict

# 前端
pwsh scripts/project.ps1 web-typecheck
pwsh scripts/project.ps1 web-test
pwsh scripts/project.ps1 web-e2e             # Playwright（需要活栈）

# 前端热重载开发（可选）：另起 API 并放开 CORS
pwsh scripts/project.ps1 web                 # http://localhost:5173，需 CORS_ORIGINS 含该源
```

`scripts/project.ps1` 的动作全集：`bootstrap` / `env-init` / `env-test` / `api` / `api-smoke` / `auth-test` / `compose-test` / `job-test` / `document-upload-test` / `application-entry-test` / `nonseed-flow-test` / `nginx-proxy-test` / `one-command-up-test` / `parser-test` / `migration-test` / `seed` / `reset-demo` / `web` / `backend-lint` / `backend-typecheck` / `backend-test` / `web-typecheck` / `web-test` / `web-build` / `web-e2e` / `verify` / `docker-recover` / `docker-recover-test`。

## 配置

所有配置见 `.env.example`。关键项：

| 变量 | 说明 |
|---|---|
| `JWT_SECRET` | ≥48 位随机串（生产必改） |
| `DATABASE_URL` / `REDIS_URL` | 默认指向 Compose 服务名 `postgres` / `redis` |
| `MOCK_MODEL_MODE` | `true` 时走确定性 FakeModel，无需真实 API Key 即可演示 |
| `MODEL_BASE_URL` / `QWEN_API_KEY` | 真实模型端点与凭据，由 Compose 透传给 API / Worker / Scheduler。`MOCK_MODEL_MODE=false` 时二者必填，缺失即启动失败；空值按「未配置」处理，不会退化成空端点 |
| `CHAT_MODEL` / `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` | 模型名与向量维度 |
| `EMBEDDING_DIMENSION` | 必须与 embedding 模型一致，默认 `1024` |
| `APPROVAL_TTL_MINUTES` | Approval 过期扫描窗口（由 Celery Beat 周期触发） |
| `STORAGE_ROOT` | 简历存储根（Compose 中挂载为卷） |
| `WEB_HOST_PORT` / `API_HOST_PORT` / `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` | 宿主侧调试端口，被 `compose.override.yaml` 使用 |

> `SSE_HEARTBEAT_SECONDS` 由 `compose.yaml` 转发，所以 `.env.example` 里的值就是容器实际取值（默认 `1` 秒）。心跳同时是撤权通知的载体：API 在每个心跳重新校验授权，缩短它才能让在线撤权在一次心跳内可见。

## 合成演示数据

`scripts/seed_demo_data.py` 通过应用自身的 ORM 模型直接写入（repository 的 `save` 即 `session.add`），严格遵守所有外键链：

- 1 个 HR 用户（已知凭据）+ 1 个 ACTIVE 岗位（含 `JobVersion` 与 `JobAssignment`）；
- 5 名候选人，各含 1 个 `READY` Profile 与 3 条 `EvidenceChunk`（确定性 1024 维 embedding）；
- 重跑安全：`docker compose --profile tools run --rm seed -- --reset` 先按标记清理再重新灌入。

> embedding 为**合成确定性向量**（由文本哈希生成），使向量召回可在离线（FakeModel）下复现；真实部署应改用配置的 embedding 网关重新生成。

## 基线与规模

| 项 | 值 |
|---|---|
| 后端 | 176 个 Python 文件（`backend/`）/ 约 28.4k 行；`ruff` 干净，`mypy` strict 通过 245 个文件 |
| 后端测试 | `pytest -q` → **865 passed**（+17 项 PostgreSQL/Redis 集成用例在无 `DATABASE_URL` 时按设计跳过，由探针栈内执行）。配置单测已与本地 `.env` 隔离，有无本地配置结论一致 |
| 前端 | 81 个 `.ts` / `.tsx` / 约 9.9k 行；`tsc -b` 干净，`vitest` 30 个测试文件 **231 passed**，`vite build` 通过 |
| E2E | 5 个 Playwright spec（含 FIN-010 非种子主路径、FIN-011 故障与安全矩阵） |
| 迁移 | 13 个 Alembic 版本，head `0013_match_explanations`，26 张表 |
| 探针 | `tests/` 下 25 个受版本控制的 PowerShell 探针，其中 **23 个接入 `project.ps1 verify`**。另 2 个是 Gate 0 的宿主环境探针（`validate_container_runtime.ps1` / `validate_environment_setup.ps1`，见 [`环境配置清单.md`](./环境配置清单.md) §5.1）：它们验证本机 Docker/WSL 与 Windows 宿主配置，因此在开发机上跑，不进 CI |
| 运行时 | Python 3.12.14 / Node 22.23.2 / PostgreSQL 17 + pgvector / Redis 7.4-alpine / Nginx 1.29.3-alpine（镜像全部固定 tag 或 digest，无 `latest`） |

## 实现进度

| 阶段 | 状态 |
|---|---|
| IMP-001 ~ IMP-030（工程骨架到发布） | ✅ 全部提交 |
| FIN-001 ~ FIN-012（幂等、Worker 基线、文档闭环、前端闭环、异步恢复、维护任务、评测、真实适配器、运行时 ADR、非种子主路径、故障矩阵、完整 Compose 与 CI） | ✅ 全部 DONE，逐项证据见 [`编码实现计划.md`](./编码实现计划.md) §20.2 / §20.3 |
| FIN-013 发布收口 | ✅ 全部 DONE：README 与环境清单校准、CI run 35066318081 全绿（含 `project.ps1 verify` 全量）、Secret/隐私扫描与依赖审计干净、`ONE_COMMAND_UP_VALIDATION_OK` / `API_RUNTIME_VALIDATION_OK` / `NGINX_PROXY_VALIDATION_OK`、release commit + `v1.0.0` tag |
| PORT-001 运行配置、依赖与测试隔离 | ✅ DONE：`httpx2` 纳入运行依赖并同步双锁文件；模型端点、凭据、模型名、超时、向量维度与 SSE 心跳由 Compose 透传；配置单测与本地 `.env` 隔离；README 与环境清单口径校准。验收证据见 [`后续开发计划.md`](./后续开发计划.md) §5 |
| PORT-002 逐项证据绑定与评分语义 | ✅ DONE：报告结论不再默认引用第一条证据，改为按观测值在候选人原文中定位（学历等级归一化到同一序数表、技能按词边界匹配、年限要求精确等值），定位不到即降级支持等级并在文案与 `confidence_note` 中说明；分数明确标注为「检索排序换算分（非模型置信度）」。验收证据见 [`后续开发计划.md`](./后续开发计划.md) §5 |
| PORT-003 真实模型匹配解释闭环 | ✅ DONE（首个验收项待凭据）：新增封闭的模型解释契约（`extra="forbid"`、`impact` 仅 `LOW/MEDIUM`，模型要求 `HIGH` 判为契约违规而非夹取）、Qwen 解释适配器与确定性 Fake；模型引用在持久化前经服务端反查切片并走同一套 §9.4 校验，模型结论支持等级上限为 `PARTIAL`；429 / 超时 / Schema 错误 / 非法引用各有独立 `reason_code`，模型故障不影响 run 终态、不触发审批或副作用；`match_explanations` 记录模型、Prompt、规则版本、耗时、重试与可获得的 Token usage，报告结论以 `source` 区分规则判定与模型解释。验收证据见 [`后续开发计划.md`](./后续开发计划.md) §5 |
| PORT-004 实际输出评测与录制回放 | ✅ DONE（真实模型项待凭据）：52 例由原始简历文本与岗位输入驱动的评测集（13 类画像 × 4 类岗位，DEV/HOLDOUT 分离），标准答案独立标注；预测由真实链路产生（网关抽取 → 召回 → 硬性规则 → 报告 → 真实 `ApplicationRun` 图），并按 `SCORER_FIXTURE` / `FAKE_MODEL` / `RECORDED_REPLAY` / `LIVE_MODEL` 分开报告、拒绝合并；28 组原始 clean / injected 文本对走同一条链路，「模型是否遵循攻击内容」与「系统是否放行越权 / 副作用 / 审批绕过」分别记录；空语料、缺录制、门禁不可达、超出调用预算一律失败退出。`python scripts/run_evaluation.py --k 5 --split holdout` 为唯一入口。验收证据见 [`后续开发计划.md`](./后续开发计划.md) §5 |
| PORT-005 关键页面与演示体验 | ✅ DONE（浏览器与投屏项未执行）：排名与报告以候选人姓名和事实摘要呈现，Profile ID 降为「辅助追踪」；每条证据标出页码 / 段落并可直接打开原文定位（`?chunk=`），证据属于旧版本时明说而不是静默失败；时间线只显示可读节点名、状态与失败原因，事件类型 / 消息键 / 载荷收进辅助详情；审批页按「拟执行动作 → 原提案与参数 → 当前版本 → 执行结果」组织，并区分「决策已记录但尚未写入」（`APPROVED` / `EDITED`）与「恰好落库一次」（`EXECUTED`）；`GET /api/v1/runtime/model-mode` 在每页顶部标明当前是真实模型还是 Mock 及其证明范围，报告标出结论来源，错误与终态各自给出恢复指引。**关键浏览器场景与实际投屏分辨率检查因本机无法启动 Docker 未执行，两项验收保持未勾选**。验收证据见 [`后续开发计划.md`](./后续开发计划.md) §5 |

## MVP 边界

- 电子 PDF / DOCX（无 OCR）、合成数据、单机、MockSchedule、精确向量检索（HNSW 不默认启用）、Langfuse 可选。
- 未审批副作用执行次数 = 0；RBAC 服务端每次重校验；SSE 每批 / 心跳持续授权。
- 尚未闭环的两项：真实百炼凭据下的模型诊断与真实模型评测（入口已就绪：`python scripts/run_evaluation.py --live --max-calls N`，缺的是凭据与预算确认，不是机制）；手动的真实模型 CI 工作流。运行镜像的模型依赖与配置透传已补齐（PORT-001），关掉 mock 时会解析到真实网关，缺凭据则在启动阶段明确失败，而不是悄悄退回 FakeModel。
- `SSE_HEARTBEAT_SECONDS` 由 Compose 转发，`.env.example`、Compose 默认值与代码默认值统一为 1 秒。

## 版本口径

三处版本号彼此独立，不要互相推导：

- **发布标签** `v1.0.0` 标记 FIN-013 发布收口那次提交，是本仓库的演示基线快照。
- **后端包**（`pyproject.toml`）与**前端包**（`apps/web/package.json`）的版本号均为 `0.1.0`，是随 PORT 系列继续迭代的工作版本，不随发布标签走。
- **API 版本**是路径前缀 `/api/v1`（`API_BASE_PATH`），属于 HTTP 契约，与上面两个包版本无关。

## 说明

本仓库为作品集级 MVP，用于展示「可验证的辅助决策链」设计：证据归属、审批隔离、可恢复性均落到可自动化评测的门禁中。运行时选择（自研 `RunEngine` 而非 LangGraph）是一次**正式接受的设计偏差**，记录在 [ADR-0001](./docs/adr/0001-agent-runtime-custom-engine-over-langgraph.md)。
