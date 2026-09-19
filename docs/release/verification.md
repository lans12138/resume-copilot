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
| 后端全量测试 | `pytest -q` | **882 passed / 17 skipped** |
| 静态检查（verify 同款） | `ruff check backend apps tests/unit tests/integration` | 干净 |
| 类型检查（verify 同款） | `mypy backend apps tests/unit` | 干净，**247 个文件** |
| E2E 夹具一致性 | `python scripts/generate_e2e_fixtures.py --check` | `E2E fixtures parse to the expected blocks.` |
| 离线评测门禁 | `python -m backend.app.evaluations.gate` | 退出 **0** |
| 离线评测报告 | `python scripts/run_evaluation.py --k 5 --split holdout` | 退出 **0**；36/36 例可测量，注入系统侧放行 **0/17** |
| 预算门禁 | `python scripts/run_evaluation.py --live` | 退出 **2**，`EVALUATION_BUDGET_NOT_STATED`（未声明预算即拒绝启动，符合预期） |
| 迁移（离线） | `alembic upgrade head --sql` | 退出 0，生成 40 条 `CREATE TABLE` / `ALTER TABLE` |
| 恢复与幂等 | `pytest -q tests/unit/test_concurrency_recovery.py tests/unit/test_side_effects.py tests/unit/test_idempotency.py` | **15 passed** |
| 前端类型 | `tsc -b` | 干净 |
| 前端测试 | `vitest run` | **30 files / 231 passed** |
| 前端构建 | `vite build` | 通过（`dist/assets/index-*.js` 383.14 kB / gzip 116.09 kB） |
| 文档通用检查 | 宿主复现 `validate_documents.ps1` 的围栏 / 表格 / 链接 / 文件数断言 | `MARKDOWN_GENERIC_OK markdown=12 local_links=50` |
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
| `pytest -q` | ✅ | 882 passed / 17 skipped（PG/Redis 集成用例按设计跳过，由探针栈执行） |
| `validate_api_runtime.ps1` | ✗ | 需要活栈 |
| web `typecheck` / `test` / `build` | ✅ | 均通过 |
| `validate_web.ps1`（Playwright） | ✗ | 需要活栈 |
| `validate_documents.ps1` | ✗ | 需要 `pwsh`；**通用断言已宿主复现**（见 §2），项目特有契约串人工核对 |
| FIN-012 Nginx 代理探针 / 一键起栈探针 | ✗ | 需要活栈 |

## 4. CI 结果

见 §5 的补充记录（在发布候选推送后填入 run 编号与结论）。

## 5. 剩余限制（明确记录，不含糊）

1. **真实模型未评测**：无百炼凭据，`--live` 未执行。入口与预算门禁已就绪（`--live` 未声明 `--max-calls` 直接退出 2），属凭据问题而非机制问题。所有报告数字均来自确定性替身，每节自带适用范围说明。
2. **PostgreSQL / Redis 集成用例本机跳过**：无 `DATABASE_URL`。跳过不等于通过——由 CI 的探针栈执行。
3. **浏览器与投屏检查未执行**：本机 Docker Desktop 起不来，Playwright 与投屏分辨率下的长文本 / 滚动区域检查均未做。因此 PORT-005 的两项验收保持未勾选，不按「组件测试通过」推断浏览器表现。
4. **录屏与截图未产出**：同上，无法起栈即无法录制。演示脚本的 5 分钟录屏版脚本已就绪，待可运行环境补录。
5. **`huawei` 远端未同步**：该远端的 `main` 停在 `99a30ab`（初始骨架）且与本地历史分叉（1 / 90），不是快进关系，未在本次发布中推送。需要时单独决定合并策略。
