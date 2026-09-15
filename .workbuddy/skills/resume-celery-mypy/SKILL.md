---
name: resume-celery-mypy
agent_created: true
description: resume-copilot 后端（FastAPI+SQLAlchemy2+Celery+pgvector）交付前的本地验证与依赖收敛工作流。当新增/修改 Celery 任务（尤其 agent Run 类任务）、引入新 pip 依赖、或跑 mypy/ruff/pytest/探针验证改动时使用；也覆盖本项目反复出现的 mypy strict 报错套路（celery 无 py.typed、SQLAlchemy scalar 返回 Any、变量函数同名、Protocol 假对象签名）、at-least-once Run 任务的「认领而非执行」模式、PS 5.1 的 ANSI 解码双坑（读文档 + 语法校验），以及 GitHub 推送凭据与 CI 读取（GCM 挂死、仓库局部 store helper、Actions run 查询）的排查。
---

# Resume Copilot - 交付前验证与依赖收敛

## Overview

本 skill 固化 resume-copilot 后端在每次 IMP 交付前的「本地隔离 venv 验证 + 依赖收敛」流程，以及本项目特有的 mypy strict 报错套路。后续 IMP-013~030（agent / evaluations / maintenance 任务全要 import celery，且会不断引入新依赖）会反复用到，照此走可省去重新踩坑。

## 环境约定（先看这个，否则命令跑不起来）

### 用 `envs/fin003`，不要用 `envs/default`

- **可用**：`C:\Users\lanqi\.workbuddy\binaries\python\envs\fin003\`（SQLAlchemy 2.0.52、mypy 1.20.2、
  ruff、pytest 齐全；2026-09-14 实测 `mypy backend apps tests/unit` → 183 files clean）。
- **已损坏**：`envs/default`——`import sqlalchemy` 抛 `cannot import name 'getcurrent' from 'greenlet'`，
  mypy 报 `No module named 'mypy.__main__'`。别用它，也别试图修它。

Windows Git Bash 里用正斜杠：`/c/Users/lanqi/.workbuddy/binaries/python/envs/fin003/Scripts/mypy.exe`

### pytest 必须在仓库外运行

仓库根的 `.env` 会被 pydantic-settings 读到，给 `STORAGE_ROOT` / `MODEL_BASE_URL` / `QWEN_API_KEY`
填上值，于是 `tests/unit/test_settings.py` 的 2 个「缺少必填字段应报错」用例不再报错 →
`2 failed, 274 passed`。**这不是代码问题。** 从仓库外跑就干净：

```bash
cd /d/code && /c/Users/lanqi/.workbuddy/binaries/python/envs/fin003/Scripts/python.exe \
  -m pytest resume/tests/unit -q          # → 302 passed
```

等价的替代方案是 Docker 开发镜像（与 CI 同构）。改动涉及真 PostgreSQL/Redis 的探针时反正要走镜像，
所以两条路都留着：

```bash
cd /d/code/resume
docker build --file deploy/docker/backend.Dockerfile --target development \
  --tag resume-copilot-backend-development:local .
docker run --rm resume-copilot-backend-development:local pytest -q tests/unit
```

### 本机门禁矩阵（哪些能本地跑，哪些只能靠 CI）

**本机没有 `pwsh`**（只有 Windows PowerShell 5.1），所以 `scripts/project.ps1 verify` **跑不了**
（它内部对子探针 `Invoke-Checked 'pwsh'`）。要逐个手动跑探针，且必须绕执行策略：

```powershell
Set-Location D:\code\resume
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\validate_web.ps1
```

| 探针 | 本机 | 说明 |
|---|---|---|
| `validate_document_pipeline.ps1` | ✅ | 真 PostgreSQL+Redis 跑 `tests/integration/test_document_pipeline_e2e.py` |
| `validate_application_entry.ps1` | ✅ | 需要 Docker；FIN-005 起会起 **api + worker**，跑完整 MatchRun（含异步等待与重试）+ 双审批链路 |
| `validate_worker.ps1` | ✅ | 需要 Docker；Worker/Beat 消费 Redis 任务 |
| `validate_web.ps1` | ✅ | Playwright 三条主路径 |
| `validate_documents.ps1` | ✅ | 2026-09-14 起修好了编码，本机可跑，输出与 CI 逐项一致 |
| `project.ps1 verify` | ❌ | 内部 `Invoke-Checked 'pwsh'`，本机无该可执行文件 |

`validate_documents.ps1` 之前只能靠 CI，原因有两个，都已修（改动对 pwsh 是 no-op）：

1. 脚本自身含中文字面量，**无 BOM 的 UTF-8 会被旧引擎按 ANSI 解码，在第一行前就 ParserError** → 文件加 UTF-8 BOM。
2. 读中文文档时 `Get-Content` 默认 ANSI，**多字节序列错位会吞掉行首**（`\n`/`~` 被当成双字节字符的尾字节），
   导致代码围栏计数失真、报出根本不存在的「围栏未闭合」→ 所有 `Get-Content` 显式 `-Encoding UTF8`。

排查这类「本机说文档坏了、CI 说没事」的问题时，先怀疑编码：用 `python` 按字节统计一遍
（如 `open(p,'rb').read().decode('utf-8')` 后数 `^~~~` 行），别信 ANSI 解码下的行结构。

**同一个坑在语法校验上会再咬一口**：PS 5.1 的 `[Parser]::ParseFile()` 也按 ANSI 读文件，于是改了
含中文的探针后，它会在一堆**早已存在、CI 长期绿**的行上报语法错，错误信息里还会出现 `'”。` 这种
半截残字。看到「报错行全是自己没碰过的老代码」就别改代码，换姿势（先显式解码再解析）：

```ps
$path = 'D:\code\resume\tests\validate_application_entry.ps1'
$text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseInput($text, $path, [ref]$null, [ref]$errors)
if ($errors) { $errors | % { 'PARSE ERROR line ' + $_.Extent.StartLineNumber + ': ' + $_.Message } } else { 'PARSE_OK' }
```

配套两个操作细节：本机 PS 工具**完全不回显 stdout**（`Write-Output "hello"` 也是空的），必须
`| Out-File -FilePath <工作区内路径> -Encoding utf8` 再读文件（写到工作区**之外**会被沙箱拦住，
表现为「命令成功但文件不存在」）；而 `*>` 出来的是 **UTF-16**，要按 `utf-16` 解码。
另外 `*> $file` 挂在 `if/else` 整块后面会被解析成只作用于 `else` 分支，结果 exit 1 且**不产生文件**——
要么给每个分支各自重定向，要么先算进一个变量再输出。

### 没有本地 pwsh，也要能验 pwsh 路径

改了探针脚本、或怀疑「本机过 / CI 不过」时，用官方镜像在真引擎里跑（本机无该可执行文件也能验）：

```bash
docker run --rm -v D:/code/resume:/repo -w /repo \
  mcr.microsoft.com/powershell:lts-debian-12 pwsh -NoProfile -File ./tests/validate_documents.ps1
```

两个坑：镜像**没有 ENTRYPOINT**，必须显式写 `pwsh`（否则报 `exec: "-NoProfile": executable file not found in $PATH`）；
`Select-Object` / 管道会吞掉失败输出，要 `*> D:\code\x.log` 落盘再按 `utf-16` 解码读。

> 别用 `& .\tests\x.ps1` 直接调，会被执行策略拦（`UnauthorizedAccess`）。`pwsh` 不存在时的报错是
> `无法将"pwsh"项识别为 cmdlet`，不是探针失败。

### 探针输出怎么读

- `*>` / `Tee-Object` 重定向出来的是 **UTF-16**，且 `Write-Host` 可能不进文件。读的时候先按 `utf-16` 解码：
  ```bash
  python -c "print(open(r'D:\code\probe.log','rb').read().decode('utf-16'))"
  ```
- `Select-String` 管道会吞掉 PS 工具的可见输出。要留证据就 `*> D:\code\x.log` 落盘再解码，**并且务必读那句 `N passed` / `failed`**——只信 exit code 会翻车（见下）。

### 只看 exit code 的教训

`validate_document_pipeline.ps1` 只在 pytest 非零退出时 throw，所以 exit 0 确实代表通过。但历史上出现过
「声称探针通过、实际断言从未跑绿」的假记录。**汇报前必须真的读到 `1 passed` 这类行。**

## 验证命令（每次交付前必跑）

```bash
V=/c/Users/lanqi/.workbuddy/binaries/python/envs/fin003/Scripts
# 1) 后端（pytest 一定要在仓库外，见上）
cd /d/code && $V/python.exe -m pytest resume/tests/unit -q
cd /d/code/resume && $V/mypy.exe backend apps tests/unit
# 2) ruff（宿主二进制，最快）
$V/ruff.exe check backend apps tests/unit tests/integration
# 3) 前端
cd /d/code/resume/apps/web && npx tsc -b && npx vitest run --reporter=basic
# 4) 改动涉及真库/浏览器链路时，再跑对应探针（Bypass + 落盘，见上表）
```

> 改到 `backend/app/candidates/` 或 `documents/` 的写路径时，**必须**跑
> `validate_document_pipeline.ps1`：只有它真正执行那 4 个 hermetic 下恒 skip 的集成用例。


## 新增 pip 依赖的标准做法

1. `pyproject.toml` 的 `dependencies`（运行时）或 `[project.optional-dependencies].dev`（开发）加一行，带上下界：`"celery>=5.6,<6.0"`。
2. 用 pip-tools 重新生成 lock（**不要手改 lock**，否则不可复现）：
   ```bash
   /c/Users/lanqi/.workbuddy/binaries/python/envs/default/Scripts/pip-compile --output-file=requirements.lock --strip-extras pyproject.toml
   /c/Users/lanqi/.workbuddy/binaries/python/envs/default/Scripts/pip-compile --extra=dev --output-file=requirements-dev.lock --strip-extras pyproject.toml
   ```
   解析较慢（>1min），放后台跑，`TaskOutput` 等完成。
3. 提交 pyproject + 两份 lock 连同实现代码，一次 commit。

> pip-compile 在 3.13 下可能顺手移除 `uvloop`（uvicorn 回退默认 asyncio 事件循环，无害）。检查 diff：用 `git diff requirements.lock | grep -E "^[-+].*==[0-9]"` 过滤掉 celery 链（celery/kombu/billiard/vine/amqp/tzdata），确认核心包（fastapi/pydantic/sqlalchemy/asyncpg）版本未漂移。

## 本项目反复出现的 mypy strict 套路（重点）

### 1. celery 无 py.typed
`from celery import Celery` / `from celery import Task` 必报 `import-untyped`。统一加 ignore：
```python
from celery import Celery  # type: ignore[import-untyped]
from celery import Task  # type: ignore[import-untyped]
```

### 2. SQLAlchemy 2.0 `session.scalar()` 返回 Any
strict `no-any-return` 会红。给变量显式标注类型，别用 `cast`：
```python
result: ResumeDocument | None = await self._session.scalar(stmt)
return result
```
（不要在 `return await self._session.scalar(stmt)` 上直接 `cast`，SQLAlchemy 2.0 stub 已能推断——cast 反而多余。）

### 3. 模块级变量与函数同名 → no-redef
缓存模式别让全局变量名和取数函数同名：
```python
_resources: RuntimeResources | None = None        # ❌ 与下方函数同名
def _resources() -> RuntimeResources: ...
# 改成：
_cached_resources: RuntimeResources | None = None  # ✅
def _resources() -> RuntimeResources:
    global _cached_resources
    if _cached_resources is None:
        _cached_resources = RuntimeResources.build(get_settings())
    return _cached_resources
```

### 4. Celery `@app.task` 装饰器 untyped
`mypy` 报 `untyped-decorator` 且函数参数缺注解：
```python
@app.task(name="documents.parse", bind=True)  # type: ignore[untyped-decorator]
def parse_document(self: Task, document_id: str, *, attempt: int, parser_version: str) -> str | None:
    ...
```

### 5. 传 session 工厂而非调用结果
repository 构造要的是工厂本身，不是一次性的 session：
```python
repository = SqlAlchemyDocumentRepository(resources.session_factory)   # ✅ 工厂
# 不是 resources.session_factory()
```

### 6. Protocol 假对象签名要全
`StorageBackend` 是 Protocol，要求 `put` / `open` / `delete_if_unreferenced` / `healthcheck` 四个齐全，且 `open` 返回 `AbstractAsyncContextManager[BinaryIO]`。测试假对象必须：方法集齐 + 返回类型匹配，否则既不满足协议又会触发 `arg-type`。只用到 `open` 时，多余的协议成员用 `...`（Ellipsis）占位实现。

## 提交纪律（AGENTS.md 硬要求）

- 每次改动**一个独立 commit**，commit message 用 `feat(scope): ...` / `fix(scope): ...`，正文列改动点 + 关联设计章节（如 §14.2）。
- 每次改动配对应测试；交付前 mypy + ruff + 相关 pytest 全绿。
- 范围外的误报（见下）单独 commit，不要混进 IMP commit。
- 不要 `git add` 整个目录；显式列出文件，过滤掉 `.workbuddy/`。

## 已知环境误报（与 IMP 无关，别在 IMP commit 里修）

**用 `envs/fin003` + 从仓库外跑 pytest，这些都不存在**（`302 passed` 的干净基线）。以下只在宿主
`envs/default` 或仓库根目录下直接跑时出现，且都是宿主环境问题，不是代码问题：

- `tests/unit/test_settings.py`：宿主 `.env` 的 `STORAGE_ROOT=/data/resumes` 在 Windows 上非绝对路径。
- `tests/unit/test_auth.py` / `test_api_smoke.py` / `test_request_contract.py`：宿主 venv 的 pydantic / starlette / anyio 版本与 lock 不一致。
- 协议漂移：`CandidateProfileRepository` 等协议加了方法但测试假对象没同步 → 单独修假对象（见 IMP-012 后的 `test_candidate_extraction.py` 修复 commit）。

## 重复枚举：跨 ORM/API 边界的 `is` 会静默失效（2026-09-14 真事故）

**症状**：CI 红在
```
assert result.status is CandidateProfileStatus.READY
AssertionError: assert <CandidateProfileStatus.READY: 'READY'> is <CandidateProfileStatus.READY: 'READY'>
```
两边 repr 完全一样，`is` 却为假。**看到这种形态，第一反应就是「同名枚举有两个类对象」。**

**真因**：`candidates/models.py` 与 `candidates/schemas.py` 各自 `class CandidateProfileStatus(StrEnum)`
定义了一遍（成员完全相同）。pydantic 构造 `CandidateProfileResponse` 时会把 ORM 成员**强制转成 schema 那个类**，
于是响应字段永远与测试从 models 导入的枚举不同一。值相等、对象不同 → `is`、`dict[enum, ...]` 查表、`match` 全部静默走错分支。

**修法**：schema 侧改为从 models 导入，单一来源。`backend` 里 `documents/schemas.py` 本来就是
`from backend.app.documents.models import DocumentStatus`——**新加枚举时照抄这个模式**。

```python
# 需要 `as` 别名，否则 mypy strict（implicit_reexport=False）报
# "does not explicitly export attribute"
from backend.app.candidates.models import CandidateProfileStatus as CandidateProfileStatus
```

**排查手法**：扫描全后端同名枚举，一次找出所有重复：
```python
import re, pathlib, collections
defs = collections.defaultdict(list)
for p in pathlib.Path('backend/app').rglob('*.py'):
    t = p.read_text(encoding='utf-8')
    for m in re.finditer(r'^class\s+(\w*(?:Status|Type|Role|State|Kind|Category|Outcome|Level))\s*\(', t, re.M):
        defs[m.group(1)].append(str(p))
print({k: v for k, v in defs.items() if len(v) > 1})
```

**回归守卫**：`tests/unit/test_status_mappings.py` 已钉住「响应模型的 status 注解 is ORM 枚举」，
新增枚举契约时可以照这个写。

## 幂等与重试分类（IMP-012 沉淀，供 agent/evaluations 任务复用）

- **业务幂等靠 DB 兜底**，不依赖 Celery `acks_late`：行锁 `SELECT ... FOR UPDATE` + `operation_key = document_id + parser_version + attempt` 守卫（同 attempt 已终态 / 新 attempt 已跑 / 版本不符 → 直接忽略）。
- Celery ack 策略：`task_acks_late=True` + `task_reject_on_worker_lost=True` + `task_acks_on_failure_or_timeout=False` + `worker_prefetch_multiplier=1`，让 at-least-once 可重放。
- 临时错误（PARSER_TIMEOUT / STORAGE_UNAVAILABLE）→ Celery 指数退避 + jitter，封顶 `max_transient_retries`；终态错误（INVALID_PDF / ENCRYPTED_PDF / INVALID_DOCX / EMPTY_TEXT / EXTRACTED_TEXT_LIMIT_EXCEEDED / UNSUPPORTED_MEDIA）→ 标 FAILED/UNSUPPORTED，绝不重试。
- Celery task 只做：参数解析 + 构建 RuntimeResources + 调应用服务；生命周期逻辑全在 `parse_service.py`（分层约束见详细设计 §14）。
- **探针里验证「重跑不重复」，不要靠单测**：唯一能证明「SQL 层的替换语义 + 异步发布链路」都对的做法，是在真库探针里制造前置状态（psql 改状态）再走真接口，并**对比重跑前后的行数**。参考 `tests/validate_application_entry.ps1` 的 retry 段：置 `FAILED/retryable` → `POST /match-runs/{id}/retry` → 轮询到 `attempt=2 且 COMPLETED` → 断言候选人/报告/claim/evidence 四类计数与重跑前逐项相等。**轮询条件要写具体终态**：重试的起点 `FAILED` 本身就是终态，只等「terminal」会立刻返回旧状态，把失败伪装成通过。

## Run 类任务：第一步是「认领」，不是「执行」（FIN-005 沉淀）

`agent_runs` 是 MatchRun/ApplicationRun 共享的聚合根，`agent.execute_match_run` / `execute_application_run` 这类任务必须写成**认领（claim）**：

1. `session.get(AgentRun, run_id, with_for_update=True)` —— 行锁。
2. 调**纯函数** `decide_claim(status, cancel_requested, intent)` 得到「本次投递是否有权执行」，把它做成独立可单测的判定（本项目在 `backend/app/agent/tasks.py`），不要散在任务体里。
3. 只有 `CLAIMED` 才继续；否则 `rollback` 并返回 `skipped + reason`（**要返回原因**，这是运维判断「卡住」的唯一线索）。
4. 拿到 `CLAIMED` 后由服务把 run 置 `RUNNING` 并清掉上一轮失败标记；**整趟执行只 commit 一次**。

本轮实现的策略表：

| 意图 | 允许状态 | 其他状态 |
|---|---|---|
| `START` | `CREATED` | 终态→跳过；`RUNNING`→跳过；`WAITING_APPROVAL`→拒绝 |
| `RETRY` | `FAILED` | 其余 `NOT_RETRYABLE` |

三个容易写错的地方：

- **取消标记优先于一切意图**：`cancel_requested_at is not None` 直接拒绝。取消的授权必须从库里读，**绝不能由任务参数决定**——谁能入队谁就能伪造参数。
- **`RETRY` 分支必须放在「终态直接跳过」之前**：`FAILED` 本身就是终态，先判终态则重试永远进不去（`ALREADY_TERMINAL`）。本项目这次就是靠「变异验证」发现这个顺序风险的：把终态判断前移，`FAILED+retry` 立刻退化为 `ALREADY_TERMINAL`，测试马上抓到。
- **`START` 绝不能接受 `WAITING_APPROVAL`**：那等于绕过人工闸门，直接违反「未审批副作用执行次数必须为 0」。这也决定了「`execute_application_run` 必须与 ApplicationRun 路由拆分**同批**落地」，不能先上一个只会 START 的版本。

配套约定：

- `operation_key = run_id:attempt`（MatchRun）/ `run_id:attempt:resume_version`（ApplicationRun，§14.1），用作 `send_task(task_id=...)`。**诚实认知**：Redis broker **不按 task_id 去重**，它只提供可观测/可审计标识；真正的守卫是上面那套 DB 认领。
- HTTP 层：`commit` 业务事实**之后**再发布任务，发布失败**只记 warning、不让请求变 5xx**（§14.4）。`CREATED` 同时表示「已入队」和「未投递」，由维护任务扫描重投。
- 重投是安全的：认领会拒绝正在执行中的 run，所以「多投一次」不会变成第二次执行。
- 派生的行（候选人快照、证据报告及其子表）**整批替换**，不追加。报告子表 FK 没有 `ON DELETE CASCADE`，要按子→父顺序显式删。

## GitHub 推送凭据与 CI 读取（2026-09-14 定型）

**结论先行：本项目不走 Git Credential Manager。** 本机 global `credential.helper` 是 PortableGit 自带的 GCM 2.9.0，它在没有有效凭据（以及某些其它状态下）会**挂死等一个不会出现的 GUI 窗口**，`GCM_INTERACTIVE=never` / `GCM_GUI=false` 都拦不住。表现为 `timeout 25 git push ...` → **exit 124、输出 0 字节**。

### 配置（仓库局部，不动 global）

```bash
printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > .git/gh-credentials
chmod 600 .git/gh-credentials
git config --local credential.helper ''                 # 空值 = 把 global 的 GCM 清出 helper 链
git config --local --add credential.helper 'store --file=<abs>/.git/gh-credentials'
```

**两条缺一不可**：只加 store 不写空值，git 仍会先问 GCM 然后挂。凭据明文存在 `.git/` 内（不会被提交，但会随 `.git` 目录被拷贝）。

### 日常三条命令

```bash
# 取 token 给 REST API 用
T=$(cut -d: -f3 .git/gh-credentials | cut -d@ -f1)

# 读 CI（注意 head_sha 过滤不稳定，拉列表自己筛）
curl -sS -H "Authorization: Bearer $T" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/lans12138/resume-copilot/actions/runs?per_page=5"

# 真验写权限（--dry-run 到已有 ref 且 up-to-date 时根本不做认证，是假成功）
git push --dry-run origin HEAD:refs/heads/_cred_probe
```

### 四个把人带偏的假象

| 假象 | 真相 |
|---|---|
| `git push ... \| tail -8; echo $?` 得到 `exit 0`，以为「静默失败」 | `$?` 是 **`tail`** 的，恒 0。push 一律 `> log 2>&1` 落盘再读 |
| `git credential fill` 能拿 token | **不可靠**，同一命令连续三次得 `93 / 0 / 0` 字符 |
| `sed 's#^https://x-access-token:/(.*/)@github.com$#/1#p'` 提取凭据 | 返回**空串** → curl 401 → 误以为 token 坏了。用 **`cut -d: -f3 \| cut -d@ -f1`** |
| `git push --dry-run` 成功 = 写权限 OK | up-to-date 时**不做认证**。要推**新 ref** 才算验过 |

附：**Windows 原生 curl 不认 Git Bash 的 `/tmp`**（`-o /tmp/x.json` → `curl: (23) client returned ERROR on write`）。要么管道给 python 读 stdin，要么给 Windows 路径。

### token 权限（够用到 FIN-013）

Contents RW（push / tag / release）、**Workflows RW**（改 `.github/workflows/*.yml` 时必须，缺则含该改动的 push 被 GitHub **整条拒绝**，FIN-012 必踩）、Actions RW（读 job 日志 / 重跑 / 取消）、Metadata R；另附 Pull requests RW、Variables RW。不给 Secrets——真实模型 key 在网页手配。
