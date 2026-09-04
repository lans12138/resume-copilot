---
name: resume-celery-mypy
agent_created: true
description: resume-copilot 后端（FastAPI+SQLAlchemy2+Celery+pgvector）交付前的本地验证与依赖收敛工作流。当新增/修改 Celery 任务、引入新 pip 依赖、或跑 mypy/ruff/pytest 验证改动时使用；也覆盖本项目反复出现的 mypy strict 报错套路（celery 无 py.typed、SQLAlchemy scalar 返回 Any、变量函数同名、Protocol 假对象签名）。
---

# Resume Copilot - 交付前验证与依赖收敛

## Overview

本 skill 固化 resume-copilot 后端在每次 IMP 交付前的「本地隔离 venv 验证 + 依赖收敛」流程，以及本项目特有的 mypy strict 报错套路。后续 IMP-013~030（agent / evaluations / maintenance 任务全要 import celery，且会不断引入新依赖）会反复用到，照此走可省去重新踩坑。

## 环境约定（先看这个，否则命令跑不起来）

- 受管 Python venv：`C:\Users\lanqi\.workbuddy\binaries\python\envs\default\Scripts\`（Windows Git Bash 用正斜杠）
  - mypy：`/c/Users/lanqi/.workbuddy/binaries/python/envs/default/Scripts/mypy`
  - ruff：`.../Scripts/ruff`
  - pip：`.../Scripts/pip`
- **不要**全局 `pip install`；缺包只装进上面的 venv。CI 用 lock 安装，本地 venv 缺包只会导致 mypy `import-not-found` / pytest 收集失败，不是代码问题。
- 声明依赖但 venv 没装的常见包：`celery`、`redis`（IMP-012 起）、`pwdlib[argon2]`（auth）、`pgvector`、`httpx2`（starlette TestClient，dev 依赖）。装完再验证。

## 验证命令（每次交付前必跑）

```bash
cd /d/code/resume
MYPY=/c/Users/lanqi/.workbuddy/binaries/python/envs/default/Scripts/mypy
RUFF=/c/Users/lanqi/.workbuddy/binaries/python/envs/default/Scripts/ruff
PY=/c/Users/lanqi/.workbuddy/binaries/python/envs/default/Scripts/python

$MYPY backend tests/unit                       # 整仓 mypy（strict）
$RUFF check backend/app/<pkg> tests/unit/<f>   # 只查本次改动文件
$PY -m pytest tests/unit/test_<imp>.py -q      # 本次 IMP 测试
# 回归：把同链路既有测试一起跑，例如 IMP-012 后跑 test_candidate_extraction.py
```

> 整仓 mypy 常因**与本次无关**的预存环境问题报红（见「已知环境误报」）。先确认本次改动文件 `mypy` 干净，再处理范围外的误报——范围外的单独 commit 修，不要混进 IMP commit。

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

- `tests/unit/test_settings.py` / `test_auth.py`：pydantic 版本错配（`literal_error` on `embedding_dimension`、token claims）——预存，未动过。
- `tests/unit/test_api_smoke.py` / `test_request_contract.py`：starlette/anyio 版本错配，anyio `DeprecationWarning` 被 `filterwarnings=["error"]` 拦下——临时 `pytest --ignore=...` 跳过跑回归。
- 协议漂移：`CandidateProfileRepository` 等协议加了方法但测试假对象没同步 → 单独修假对象（见 IMP-012 后的 `test_candidate_extraction.py` 修复 commit）。

## 幂等与重试分类（IMP-012 沉淀，供 agent/evaluations 任务复用）

- **业务幂等靠 DB 兜底**，不依赖 Celery `acks_late`：行锁 `SELECT ... FOR UPDATE` + `operation_key = document_id + parser_version + attempt` 守卫（同 attempt 已终态 / 新 attempt 已跑 / 版本不符 → 直接忽略）。
- Celery ack 策略：`task_acks_late=True` + `task_reject_on_worker_lost=True` + `task_acks_on_failure_or_timeout=False` + `worker_prefetch_multiplier=1`，让 at-least-once 可重放。
- 临时错误（PARSER_TIMEOUT / STORAGE_UNAVAILABLE）→ Celery 指数退避 + jitter，封顶 `max_transient_retries`；终态错误（INVALID_PDF / ENCRYPTED_PDF / INVALID_DOCX / EMPTY_TEXT / EXTRACTED_TEXT_LIMIT_EXCEEDED / UNSUPPORTED_MEDIA）→ 标 FAILED/UNSUPPORTED，绝不重试。
- Celery task 只做：参数解析 + 构建 RuntimeResources + 调应用服务；生命周期逻辑全在 `parse_service.py`（分层约束见详细设计 §14）。
