# 发布验收记录（PORT-006）

同一代码版本上的最终检查记录。**没有跑的东西一律标为未执行**，不用「等价方式已验证」含糊过去。

- 发布标签：`v1.1.0`（标记 PORT-001~006 这一轮改进的收口提交；`v1.0.0` 是 FIN-013 的第一个基线快照）
- 发布候选：本文件所在提交（tag 指向的提交）
- 上一版基线：`v1.0.0`
- 评测报告：[`../evaluation/report-holdout.md`](../evaluation/report-holdout.md)（报告自带「溯源」块，写明生成它的 commit 与命令）

## 1. 验收标准逐条对照

| # | 验收标准 | 结论 | 证据 / 未执行原因 |
|---|---|---|---|
| 1 | 全新环境能够按文档启动并完成合成文件主流程 | ✅ DONE | 起栈路径与三项健康检查见 [`演示脚本.md`](../../演示脚本.md) §0；主流程（上传 → 解析 → 校对 → 批量匹配 → 双审批 → 面试）在两个演示版本里逐幕写明并标注权威信号。**本机未实跑**：Docker Desktop 起不来，实际启动由 CI 的 `project.ps1 verify` 覆盖（含 `validate_nonseed_flow.ps1` 非种子主路径探针） |
| 2 | `pwsh scripts/project.ps1 verify` 通过，包含 PostgreSQL / Redis 集成、前端构建与浏览器验证 | ⏳ 由 CI 执行 | 本机无 `pwsh`，Docker Desktop 不可用，因此整条 `verify` 只能由 CI 跑。CI 与本地共用同一入口，不存在「本地绿、CI 红」的分叉。逐步骤对照见 §3 |
| 3 | 模型效果报告能追溯到发布候选 commit 和评测版本 | ✅ DONE | 报告新增「溯源」块：commit（不可读时写「未记录」并说明不能归属）、工作区是否干净（三值：干净 / 有未提交改动 / 无法确认）、生成时间、**实际执行的命令**。评测版本原本就有（数据集 content_hash、提示词 / 规则 / 网关 / 向量标识）。实现与失败路径测试见 `backend/app/evaluations/provenance.py` + `tests/unit/test_evaluation_provenance.py`（17 项） |
| 4 | 录屏、截图、指标与当前代码一致，公开材料只含合成数据 | ⚠️ 部分 | 指标一致：报告在收口提交上重新生成，README 的关键指标与该报告同源。合成数据：演示语料、`e2e-candidate-resume.pdf`、DEMO 岗位与 5 名候选人均为合成，仓库不含真实简历。**录屏与截图未产出**：本机起不了栈，无法录制 |
| 5 | 副作用审批、重复投递、取消、恢复与 SSE 撤权契约没有回归 | ✅ DONE | 15 项恢复与幂等测试通过（见 §2），案例与断言位置见 [`no-duplicate-write.md`](./no-duplicate-write.md)；SSE 撤权契约由 `validate_web.ps1`（Playwright FIN-011 故障矩阵）在 CI 覆盖，本机未执行 |
| 6 | 发布材料说明真实模型与 Mock 模式分别能证明什么 | ✅ DONE | README「模型与规则边界」、演示脚本「附 A：Mock 模式的验证范围」、评测报告每节自带的适用范围说明，三处口径一致 |

## 2. 本机执行的验证

| 项 | 命令 | 结果 |
|---|---|---|
| 后端全量测试 | `pytest -q` | **886 passed / 17 skipped** |
| 静态检查（verify 同款） | `ruff check backend apps tests/unit tests/integration` | 干净 |
| 类型检查（verify 同款） | `mypy backend apps tests/unit` | 干净，**247 个文件** |
| E2E 夹具一致性 | `python scripts/generate_e2e_fixtures.py --check` | `E2E fixtures parse to the expected blocks.` |
| 离线评测门禁 | `python -m backend.app.evaluations.gate` | 退出 **0** |
| 离线评测报告 | `python scripts/run_evaluation.py --k 5 --split holdout` | 退出 **0**；36/36 例可测量，注入系统侧放行 **0/17** |
| 预算门禁 | `python scripts/run_evaluation.py --live` | 退出 **2**，`EVALUATION_BUDGET_NOT_STATED`（未声明预算即拒绝启动，符合预期） |
| 迁移（离线） | `alembic upgrade head --sql` | 退出 0，生成 40 条 `CREATE TABLE` / `ALTER TABLE` |
| 恢复与幂等 | `pytest -q tests/unit/test_concurrency_recovery.py tests/unit/test_side_effects.py tests/unit/test_idempotency.py` | **15 passed** |
| 前端类型 | `tsc -b` | 干净 |
| 前端测试 | `vitest run` | **31 files / 239 passed** |
| 前端构建 | `vite build` | 通过（`dist/assets/index-*.js` 383.16 kB / gzip 116.09 kB） |
| 文档通用检查 | 宿主复现 `validate_documents.ps1` 的围栏 / 表格 / 链接 / 文件数断言 | `MARKDOWN_GENERIC_OK markdown=12 local_links=60` |
| 演示脚本契约串 | 逐条 `grep -F` 核对 9 个必需串 + 2 条负向断言 | 9/9 命中，负向断言 0 命中 |

## 3. `project.ps1 verify` 逐步骤对照

`verify` 是一条顺序脚本。下表把它的每一步标出本机是否执行——**未执行的都是 PowerShell 探针或需要活栈的步骤**，不是被跳过就算过。

| verify 步骤 | 本机 | 说明 |
|---|---|---|
| `validate_project_structure.ps1` | ✗ | 需要 `pwsh` |
| `check_probe_python.ps1` | ✗ | 需要 `pwsh` |
| `validate_docker_recovery.ps1` | ✗ | 需要 `pwsh` + Docker |
| `validate_local_env.ps1` | ✗ | 需要 `pwsh` |
| `validate_compose_stack.ps1` | ✗ | 需要活栈 |
| `validate_migrations.ps1` | ✗ | 需要 `pwsh`；**离线等价检查已执行**（见 §2） |
| `validate_idempotency.ps1` | ✗ | 需要活栈 |
| `validate_document_pipeline.ps1` | ✗ | 需要活栈 |
| `validate_maintenance.ps1` | ✗ | 需要活栈 |
| `validate_evaluations.ps1` | ✗ | 需要 `pwsh` + 探针镜像 |
| `validate_evaluation_gate.ps1` | ✗ | 需要 `pwsh`；**门禁本体已执行**（退出 0） |
| `validate_model_adapter.ps1` | ✗ | 需要 `pwsh` |
| `validate_runtime_model_adapter.ps1` | ✗ | 需要构建运行镜像 |
| `validate_worker.ps1` | ✗ | 需要活栈 |
| `validate_auth.ps1` | ✗ | 需要活栈 |
| `validate_document_upload.ps1` | ✗ | 需要活栈 |
| `validate_application_entry.ps1` | ✗ | 需要活栈 |
| `validate_nonseed_flow.ps1` | ✗ | 需要活栈 |
| `generate_e2e_fixtures.py --check` | ✅ | 退出 0 |
| `ruff check backend apps tests/unit tests/integration` | ✅ | 干净 |
| `mypy backend apps tests/unit` | ✅ | 247 文件 |
| `pytest -q` | ✅ | 886 passed / 17 skipped（PG/Redis 集成用例按设计跳过，由探针栈执行） |
| `validate_api_runtime.ps1` | ✗ | 需要活栈 |
| web `typecheck` / `test` / `build` | ✅ | 均通过 |
| `validate_web.ps1`（Playwright） | ✗ | 需要活栈 |
| `validate_documents.ps1` | ✗ | 需要 `pwsh`；**通用断言已宿主复现**（见 §2），项目特有契约串人工核对 |
| FIN-012 Nginx 代理探针 / 一键起栈探针 | ✗ | 需要活栈 |

## 4. CI 结果

发布候选的**代码版本**由 CI 完整跑过，两个 job 全绿：

- run [`35443075182`](https://github.com/lans12138/resume-copilot/actions/runs/35443075182)（提交 `c23642ac`）
  - `Static gates (docs, structure, TOML/JSON)`：通过 —— `validate_documents.ps1` + `validate_project_structure.ps1`
  - `Reproducible project gate`：通过 —— 即 `pwsh scripts/project.ps1 verify` 的完整入口。**§3 中标 ✗ 的每一步都在这里真实执行过**，不再有「只靠推演」的步骤。

### 4.1 之前从未取得结果的步骤

| 探针 | CI 输出（摘录） |
|---|---|
| `validate_web.ps1`（Playwright） | `WEB_VALIDATION_OK browser=chromium flow=login-match-application-dual-approval-interview flow=upload-review-readiness storage=session-only`；`18 passed (52.2s)` |
| `validate_documents.ps1` | `DOCUMENT_VALIDATION_OK markdown=12 requirements=162 resources=27 api_methods=46 detailed_sections=24 contracts=10 plan_sections=21 work_packages=30 planned_days=27 plan_contracts=16 environment_sections=19 environment_variables=40` |
| Nginx 代理探针（FIN-012） | `NGINX_PROXY_VALIDATION_OK entry=nginx:8080 static=served+spa-fallback+immutable-assets security_headers=nosniff+csp+frame-deny bearer=forwarded+api-rejects-anonymous sse=streamed+chunked+frames-51+heartbeat-0.004s revocation=enforced` |
| 一键起栈探针（FIN-012） | `ONE_COMMAND_UP_VALIDATION_OK project=resume-copilot-fin012-onecommand-probe entry_port=18081 seed=1\|5 cleanup=containers+volumes+networks-removed` |
| `validate_api_runtime.ps1` | `API_RUNTIME_VALIDATION_OK image=resume-copilot-api:local status=ok` |
| `validate_nonseed_flow.ps1` | `NON_SEED_FLOW_OK run=8e35ef438ad042f1bae9703047f66208 document=88030c33-14bd-4613-b74a-0164f765b12b profile=4b53042c-356f-4a5d-a8d2-51c2f0d756c9` |

同一 run 里其余探针各打印一条 `*_OK`，共 17 条。**PostgreSQL / Redis 侧的集成在这里真实跑过**，不是跳过：

```
FIN-001 idempotency PostgreSQL integration tests passed.
FIN-002 worker/scheduler Redis integration probe passed.
FIN-006 maintenance integration tests passed.
FIN-007 evaluation integration tests passed.
FIN-007 offline evaluation gate passed.
```

### 4.2 CI 与本机数字对照（同一代码版本）

| 项 | 本机（§2） | CI |
|---|---|---|
| `pytest -q` | 886 passed / 17 skipped | 885 passed / 18 skipped |
| `vitest run` | 31 files / 239 passed | 31 files / 239 passed |
| `mypy` | 247 文件 | `Success: no issues found in 247 source files` |
| Playwright | 未执行 | 18 passed |

`pytest` 两边总数一致（903），差别只有一条用例：`tests/unit/test_fin009_adr_contract.py:97` 在 CI 跳过、在本机通过。原因与影响见 §5 第 6 条。

17 条 `DATABASE_URL` 集成用例在**两边都**跳过——CI 的 `pytest` 步骤同样不注入 `DATABASE_URL`——它们由同一 job 的活栈探针覆盖，证据是 4.1 里那五条 `FIN-00x … passed.`。

本文件与评测报告所在的收口提交，与 `c23642ac` 的**代码部分完全一致**（只相差文档与报告的溯源块），因此这次 run 的结果对应发布候选本身。

## 5. 剩余限制（明确记录，不含糊）

1. **真实模型未评测**：无百炼凭据，`--live` 未执行。入口与预算门禁已就绪（`--live` 未声明 `--max-calls` 直接退出 2），属凭据问题而非机制问题。所有报告数字均来自确定性替身，每节自带适用范围说明。
2. **PostgreSQL / Redis 集成用例本机跳过**：无 `DATABASE_URL`。跳过不等于通过——由 CI 的探针栈执行。
3. **浏览器与投屏检查未执行**：本机 Docker Desktop 起不来，Playwright 与投屏分辨率下的长文本 / 滚动区域检查均未做。因此 PORT-005 的两项验收保持未勾选，不按「组件测试通过」推断浏览器表现。
4. **录屏与截图未产出**：同上，无法起栈即无法录制。演示脚本的 5 分钟录屏版脚本已就绪，待可运行环境补录。
5. **`huawei` 远端未同步**：该远端的 `main` 停在 `99a30ab`（初始骨架）且与本地历史分叉（1 / 90），不是快进关系，未在本次发布中推送。需要时单独决定合并策略。
6. **CI 里 `requirements.lock` 的 LangGraph 断言被跳过**：`pytest -q` 由 `Invoke-BackendTool`（`scripts/project.ps1:240`）在**后端开发镜像**内执行——`docker run --rm <dev image> pytest -q`，没有挂载工作区——而 `deploy/docker/backend.Dockerfile` 的开发阶段只 `COPY requirements-dev.lock`（第 11 行），`requirements.lock` 仅出现在运行阶段（第 33 行）。于是 `tests/unit/test_fin009_adr_contract.py:97` 的 `test_no_langgraph_dependency_is_declared[requirements.lock]` 在 CI 报 `requirements.lock not present in this checkout` 并跳过：ADR-0001 §2.1「运行时不声明 LangGraph」这条守卫，在 CI 里对**唯一真正定义运行时依赖的那个文件**没有生效（另外两个参数 `pyproject.toml`、`requirements-dev.lock` 正常断言）。本轮发布候选在这一条上**是被验证过的**——本机全量 `pytest` 执行了它并通过（§2）——但后续任何只改 `requirements.lock` 的改动都可能绕过 CI。**未在本轮修复**：改 Dockerfile 会改变门禁本身，而本机无 Docker、无法自验证，因此留作独立改动；最小修法是给开发阶段加一行 `COPY requirements.lock ./`。
