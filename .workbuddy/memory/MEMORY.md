# 项目记忆：resume-copilot（企业招聘 Copilot）

## 定位与硬约束
六周单人开发的「作品集级 MVP」，目标岗位 AI Agent 开发。面向 HR 的**辅助**招聘系统，核心是可验证的辅助决策链：整理简历→可评测召回→带证据解释匹配→批量分析与单人审批隔离→副作用前暂停→故障可恢复。

1. **证据链一等公民**：确定性结论必须引 `EvidenceChunk`（精确摘录 + SUPPORTED/PARTIAL/INSUFFICIENT）；引用非法或证据不足→降级「不足以判断」。
2. **人工审批 + 幂等**：副作用前必须 interrupt 等人审批；未审批副作用执行次数 = 0；同一审批只决定一次。
3. **可恢复性**：Checkpoint 恢复、协作式取消、Approval 过期重试。
4. 文档/岗位描述按**不可信数据**：不得改变控制流、权限或工具参数（IMP-027 配对评测门禁守护）。

## 技术栈
React+TS+Vite+AntD+React Flow+ECharts / TanStack Query+Zustand；Python 3.12+FastAPI、Pydantic v2、SQLAlchemy2+Alembic、PG17+pgvector(1024)、Redis7+Celery。
Agent 是自研 `RunEngine` + `SqlCheckpointer`（**不是 LangGraph**，ADR-0001）。模型 `qwen3.7-plus` / `qwen3.7-text-embedding`；默认 `MOCK_MODEL_MODE=true`。
部署 Docker Compose + Nginx —— **`web` 服务是「Node 构建→Nginx 运行」两阶段镜像，没有独立 `nginx` 服务**，它是唯一公开入口；CI GitHub Actions。
规模：backend 153 py / 1.8 万行；web 69 个 ts(x)；5 个 e2e spec；迁移 head `0012_evaluation_tables`（25 表）。

## 当前状态（2026-09-16 11:50）
- HEAD `94e8579`（main，已推远端）。**FIN-001~012 全部 DONE**；逐项证据（commit/测试/探针/踩坑）在 `编码实现计划.md` §20.2 总表 + §20.3 分项清单，此处不重复。
- **只剩 FIN-013 发布收口**：README / 环境清单按实际状态重写、固化演示/重置/诊断命令、Secret 与隐私扫描、最终 `project.ps1 verify`、release commit + tag。
- **CI run `35049955038` 红了**，根因两处已修并推送（commit `e871de2`）：① `validate_nonseed_flow.ps1` multipart 字段名 `file` → `files`（API 声明 `files: list[UploadFile]`，原写法 422）；② `start_stack.ps1` 占位符门禁曾误杀 inert 占位符（只该挡 `JWT_SECRET`/`POSTGRES_PASSWORD`/`DATABASE_URL` 三个阻塞级）。
- **第三个待修缺陷**：非种子探针不等校对的 pin 步骤就等 embedding。**证据块是 HR 在校对页手动 pin 的**（`DocumentReviewPage.tsx` 调 `api.pinEvidence`），不是解析后自动生成；worker 日志 `embeddings.generate_chunks` 返回 `generated:0`、`chunks_for_profile=0`，故探针永远等不到。

## 核心架构约束（不可妥协）
1. **单 Agent 双运行图**：MatchRun = 岗位级批量分析（不审批、不副作用）；ApplicationRun = 单候选人固定 8 节点双审批图（`human_review` 主中断 + `wait_schedule_approval` 条件中断）。隔离靠 JobApplication 活动 Run 排他槽 + Approval 幂等键。
2. 副作用节点只埋 typed state；真正写表由 SideEffect 服务经 Approval 状态机 + 乐观锁 CAS + `idempotency_key` 保证**只执行一次**。
3. RBAC 服务端每次重校验；SSE 每批/心跳持续授权。
4. MVP 边界：电子 PDF/DOCX（无 OCR）、合成数据、单机、MockSchedule、精确向量检索（HNSW 不默认）、Langfuse 可选。
5. 不可削减项：证据归属/摘录/语义支持、JobAssignment 资源级授权 + SSE 持续授权、活动槽/Approval/幂等/副作用隔离、Checkpoint 恢复 + 协作取消 + Approval 过期、Prompt Injection 配对回归 + 核心自动化测试。

## 陷阱清单（踩过就别再踩）

### Agent 运行时与工作流
- 运行时是自研引擎不是 LangGraph（ADR-0001 已正式接受偏差，`langgraph` 零依赖零导入）；`tests/unit/test_fin009_adr_contract.py` 守「零依赖」+「需求不得强制特定第三方库」。易错事实：ApplicationRun 中断态是 `WAITING_APPROVAL`（非 `INTERRUPTED`）；审批业务 ordinal 是 **1 和 2**（非 0）；幂等键 `run_id:attempt:action_type:ordinal`。
- **at-least-once 任务第一步是「认领」不是「执行」**：先 `FOR UPDATE` 锁聚合根，再用**纯函数**判定本次投递是否有权执行（状态×取消标记×意图），行锁持到终态提交；授权判断必须读库里的标记，不能信任务参数。
- **同一 run 重跑要「替换派生行」而非追加**：`match_run_candidates`/`match_reports`（子表 `report_claims`/`claim_evidences`，FK 无 CASCADE，按子→父序删）。追加必撞唯一约束并整趟回滚，重试永远完不成。
- **业务结论 ≠ 工程故障**：阈值未命中 = `COMPLETED`+`passed=False`；飞架损坏 = `FAILED`+安全错误码，不许合并。门禁只对**带阈值**的指标取合取，**空指标集必须判不通过**（`all()` 对空列表返回 True 会造「没跑检查却显示通过」的假绿灯；`gate_passed` 与 `EvaluationService.complete` 两处都修过）。

### 数据模型 / Python
- status 列必须 `Enum(Model, native_enum=False, create_constraint=False, length=32)`，裸 `String(32)` 只有真 DB 才暴露（ORM 读回 `str`、`is` 失效、`.value` AttributeError；内存假仓储永远绿）。守卫 `test_status_mappings.py`。
- **同一枚举绝不能定义两次**（models 一份 + schemas 一份）：值相等的两个类对象永不 `is` 相同，跨 ORM/API 边界的 `is`、`dict[enum,...]` 查表、`match` 静默走错分支，且两边 repr 一样极具误导。schemas 已改为从 models 导入（`as` 别名满足 mypy strict）。
- `mypy` 也查测试文件：`# noqa: ANN001` 骗得过 ruff 骗不过 mypy；正解 PEP 695 泛型 `def _run[T](coro: Coroutine[Any, Any, T]) -> T:`（必须 `Coroutine` 不是 `Awaitable`）。
- `pytest.mark.anyio` 在本仓库会**静默跳过全部测试**（没装 anyio 插件）；用「测试内嵌 `async def _run()` + `asyncio.run(_run())`」。
- 测试里调自带 `asyncio.run()` 的同步包装器（Celery task、`python -m` 入口）要 `asyncio.to_thread(...)` 或子进程隔离。
- `caplog` 拿不到结构化日志字段；断言凭据不外泄要直接挂 `logging.Handler` 扫 `record.getMessage()` + `vars(record)`。
- 隐式字符串拼接比 `*` 结合更紧：`"=" * 68` 会把**整条已拼接串**重复 68 次；多行文本用 `"\n".join([...])` + 独立变量存重复片段。
- `EmbeddingDimensionError` 定义在 `infrastructure/embedding.py`，`candidates/embedding_service.py` 只是再导出；从后者 import 会让 mypy 报 attr-defined。
- `mock_model_mode` 开关必须由 chat / embedding 两个工厂给出一致答案（历史上一处静默返回 Fake、一处抛 `NotImplementedError`）。
- 结构化输出一律当**不可信输入**：先 `json.loads` 再 `model_validate`，schema 违例判**永久**失败；`ValidationError` 必须包成 `ChatCompletionShapeError`，否则 Celery 会去重试注定失败的回调。
- Embedding 是**位置对应**的：按 provider 的 `index` **重排**并拒绝重复/缺口/非数值（静默信任会让检索悄悄错且不报错）。

### 前端 / SSE / E2E
- SSE 线格式以 `backend/app/sse/schemas.py` 为准：`event: <type>\nid: <sequence>\ndata: <json>\n\n`；心跳 `:\n\n`；撤销 `event: SSE_AUTH_REVOKED\ndata: {"reason":"access_revoked"}\n\n`。心跳间隔 `sse_heartbeat_seconds` 默认 **1 秒**。
- **`stop()` 之后再 `open()` 是死代码**：`stop` 置 `closed=true` 并 `abort()`，而 `open()` 开头 `if (closed) return`。中止不变量必须逐次可重建（`const controller` → `let`，被 abort 过的 controller 会毒化后续所有尝试）；要写一条断言「第二次请求真的发生了」的测试。
- `page.route` 的 glob 不匹配**不报错**，只让你测个寂寞：筛选为默认 `"ALL"` 时 `listJobs` 请求 `/api/v1/jobs` **不带查询串**，`**/api/v1/jobs?**` 永远匹配不上、请求穿透真 API 变假绿。用正则 `/\/api\/v1\/jobs(\?.*)?$/`。
- `page.route` 里读请求头要用 `await request.allHeaders()`（`request.headers()` 会把名字小写化，读 `Last-Event-ID` 踩空）。
- 别在测试里指向真实端口做「预计失败」的断言（如 `http://127.0.0.1:1`）：会真建 socket，实测把一个文件从 0.9s 拖到 9s；用 `httpx2.MockTransport`。

### 「本机绿、CI 红」（干净 runner 才暴露）
- **探针 teardown 必须带 `--profile tools`**：`storage-init` 在 tools profile，compose 看不见它 → 卷 `_resume_storage` 报 `Resource is still in use` 删不掉。`validate_project_structure.ps1` 有静态守卫（扫 `.ps1`，`'down',` 前后 480 字符窗口内必须有 `'--profile','tools'`）。
- `tests/check_probe_python.ps1` 曾假设本地已有 `resume-copilot-backend-development:local`（靠 warm cache 才过）；已改用 digest 固定的 `python:3.12.14-slim-bookworm@sha256:782412e8...`。守卫：凡提到 `<name>:local` 的脚本必须在**同一文件**里 `--tag` 打出来。
- `backend.Dockerfile` 的 development target 必须 `COPY docs ./docs` + `COPY *.md ./`（`test_fin009_adr_contract.py` 要读仓库根文档），漏了就是「本机绿、容器红」。
- 容器内迭代集成测试别每次 rebuild：`-v /d/code/resume/tests:/workspace/tests:ro` + `PYTHONDONTWRITEBYTECODE=1`；自建脚本要 `PYTHONPATH=/workspace`（镜像 WORKDIR 是 `/workspace` 不是 `/app`）。
- 改文档/探针前先跑 `tests/validate_documents.ps1` 与 `scripts/check_powershell_syntax.ps1`。

### Windows / PowerShell（本机）
- **PS 5.1 不能用来验证项目门禁**：宿主不维护 `$LASTEXITCODE`（`& docker bogus-arg` 之后仍是 0）；`check_powershell_syntax.ps1` 全量跑报 4 个文件 PARSE_ERR（它们用「行首管道符」，PS 7 才支持，CI 用 pwsh 7 能过）。**别把这类文件算进本机语法门禁。**
- **PS 没有反斜杠转义**：双引号串里 `\"` 被读成裸引号，字符串提前闭合，报错行与起因相距很远还级联。要内嵌引号就换结构——here-string（`@"..."@`，`"@` 必须在行首无缩进），内嵌语言自己拼引号（Python 用 `chr(34)`）；`@'...'@` 才完全字面化。
- PS 5.1 的 `[Parser]::ParseFile()` 按 ANSI 读文件 → 对含中文的文件报**假**语法错；正解是 `[System.IO.File]::ReadAllText($p,[Text.Encoding]::UTF8)` 再 `ParseInput`。`Get-Content` 一律显式 `-Encoding UTF8`；脚本文件需 UTF-8 BOM。
- PS 5.1 不接受 here-string 直接作数组字面量元素，先赋值给变量再传参。
- **工具会话不回显 stdout、也不维护原生命令的 `$LASTEXITCODE`**（`npm run typecheck` 拿到空串）。PS 工具结果必须 `| Out-File -Encoding utf8` 落盘再 Read；每次调用都是新会话，`Set-ExecutionPolicy -Scope Process Bypass` 要每次重设。要拿真实退出码/输出就套上面的 pwsh runner。
- 从 Bash 工具调 PowerShell 会被安全策略拒绝；**Bash 工具本身不可用**（`dirname`/`ls`/`wc`/`head` 全 command not found，`/d/code/...` 路径也不认）。本仓库一律用 PowerShell 工具 + Glob/Grep/Read。
- 删除文件被工作区 safe-delete 钩子拦（`SAFE_DELETE_BULK_GUARD_ERROR` / `..._CONFIRM_REQUIRED`）；临时文件可留在仓库根或用 `.git/info/exclude` 屏蔽。

### git（本仓库高危）
- **⛔ 绝不要用 `git stash` 做验证性对比**：2026-09-15 命令被 SIGTERM 打断后 `.git/refs` 被整体删除、pack 丢失而 `.idx` 残留，**全部本地历史不可恢复**。对照基线用 `git show HEAD:<file>` 或 `cp -r` 到临时目录。已有两次损坏记录（0902、0915），`gc.auto 0` 与 `pruneExpire=never` 为此而设；别用 `git commit --amend` 改非 HEAD 提交（报成功但 SHA 未变）。
- **新建 ref 会被静默吞掉**（沙箱）：`git tag` / `update-ref` / `fetch` 建 ref 返回 0 但 ref 不落盘；绕法是手写 41 字节 SHA + 换行到 `.git/refs/...`。
- **`git add <文件> && git commit` 会带走 index 里已暂存的东西**：工作区有 `M `（已暂存）文件时，第一次提交会吞掉无关改动。先 `git reset <base>`（mixed）清 index，再逐批 add。
- 别用管道取 `git push` 的 `$?`（`| tail` 吃掉它恒为 0）；`> log 2>&1` 落盘再读。判定是否真推上去**只认 `git ls-remote origin main`**，不要信本地 `origin/main`。
- 详见技能：对象库损坏诊断/恢复 → `git-object-store-recovery`；CI 失败定位与非交互取凭据 → `gha-failure-triage`；push 凭据（两把钥匙、`git -c credential.helper=` 清空 helper 链、`x-access-token` 当用户名）→ 用户级 MEMORY.md。

### 本机环境
- **Docker**：Desktop 在 `D:\develop\Docker`；compose 插件不在 PATH（在 `C:\Program Files\Docker\cli-plugins\docker-compose.exe`，复制到 `~/.docker/cli-plugins/` 后可用）。跑自己的栈前注意遗留项目占着 5433/6380/8000，要显式设 `POSTGRES_HOST_PORT`/`REDIS_HOST_PORT`/`API_HOST_PORT`/`WEB_HOST_PORT`。
- **Python venv**：`envs/fin003` 与 `envs/default` **均已不可用**。做法是 uv 现建现删（项目 pin `>=3.12,<3.13`，不能用 uv 默认的 3.13）：`uv venv --python "C:/Users/lanqi/AppData/Roaming/uv/python/cpython-3.12.14-windows-x86_64-none/python.exe" <repo>/.venv-check`；**editable 安装会撞沙箱 safe-delete 守卫**（「Build failures...」是假线索），改为先装 httpx2/mypy/pytest/ruff，再显式装 pyproject 里那 14 个运行时依赖；设 `UV_LINK_MODE=copy`。**用完 `rm -rf .venv-check`**。
- **pytest 必须从仓库外跑**：仓库根 `.env` 会污染 `test_settings.py` 的 2 个用例（`storage_root must be an absolute path`）。`cd /d/code && /d/code/resume/.venv-check/Scripts/python.exe -m pytest resume/tests/unit -q`。
- **pwsh 7.6.6 便携版在 `D:\develop\pwsh-7.6.6\pwsh.exe`，工具会话能真跑项目门禁**（旧记录「拉不起子进程」是错的）。正解：把内层逻辑写成纯 ASCII 的 runner `.ps1`，`& '<full>\pwsh.exe' -NoProfile -ExecutionPolicy Bypass -File <runner.ps1> *>&1 | Out-File <log> -Encoding utf8`，再 Read 那个 log。**别用 `-Command`**：双引号会被外层剥掉，`$(...)` 直接变成命令名报错。
- **子进程 pwsh 的控制台编码继承父会话（本机 = gb2312）**：docker 的 UTF-8 输出会被按 GB2312 解 → 乱码 → `ConvertFrom-Json` 中途炸，报错位置正好落在第一个中文处（「本机红、CI 绿」）。runner 开头必须 `[Console]::OutputEncoding = [Text.Encoding]::UTF8`。
- **编辑 `.ps1` 会丢掉 UTF-8 BOM**（HEAD 有、改完没有）。补回：`[System.IO.File]::WriteAllText($p,$t,(New-Object System.Text.UTF8Encoding($true)))`。
- `pyproject.toml` 的 `requires-python` 仍是 `>=3.12,<3.13`，与历史运行环境（3.13.12）矛盾，未改；CI 用 lock 安装故无碍。

## 文档地图
需求分析.md、概要设计说明书.md、详细设计说明书.md、技术栈选型与架构决策.md、编码实现计划.md（IMP-001~030 + 周门禁 G1~G6；§19.2 诚实 PARTIAL 域、§20.2 FIN 总表、§20.3 分项验收清单、§20.5 完工判定）、UML规划文档.md、项目可行性分析.md、环境配置清单.md、README.md、`docs/adr/0001-agent-runtime-custom-engine-over-langgraph.md`。

## 约定
每次改动配独立 Git commit（AGENTS.md 硬要求）+ 对应测试，交付前确保测试与门禁全通过。（「宋体四号 / 1.5 倍行距 / 字数区间」只适用于学校 SRS 文档，与本项目无关。）
