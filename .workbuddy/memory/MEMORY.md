# 项目记忆：resume-copilot（企业招聘 Copilot）

> 操作级陷阱清单已拆到本目录的 **`PITFALLS.md`**（连接池/运行时/数据模型/前端 SSE/PowerShell/git/本机环境）。改代码或探针前先看那一份，本文件只留定位与状态。

## 是什么
六周单人开发的「作品集级 MVP」，目标岗位 AI Agent 开发。面向 HR 的**辅助**招聘系统，主线是可验证的辅助决策链：整理简历→可评测召回→带证据解释匹配→批量分析与单人审批隔离→副作用前暂停→故障可恢复。

四条硬约束：① 确定性结论必须引 `EvidenceChunk`（精确摘录 + SUPPORTED/PARTIAL/INSUFFICIENT），引用非法或证据不足则降级「不足以判断」；② 副作用前必须 interrupt 等人审批（未审批副作用执行次数 = 0，同一审批只决定一次）；③ 可恢复：Checkpoint 恢复 + 协作式取消 + Approval 过期重试；④ 文档/岗位描述一律按**不可信数据**（不得改变控制流、权限或工具参数）。

## 技术栈
React+TS+Vite+AntD+React Flow+ECharts / TanStack Query+Zustand｜Python 3.12+FastAPI、Pydantic v2、SQLAlchemy2+Alembic、PG17+pgvector(1024)、Redis7+Celery｜自研 `RunEngine`+`SqlCheckpointer`（**不是 LangGraph**，ADR-0001）｜Docker Compose，`web` 是「Node 构建→Nginx 运行」两阶段镜像且**是唯一公开入口**（没有独立 `nginx` 服务）｜CI GitHub Actions。规模：backend 153 py / 1.8 万行，web 69 个 ts(x)，5 个 e2e spec，迁移 head `0012_evaluation_tables`（25 表）。

## 当前状态（2026-09-16 17:00）—— **项目已完工，v1.0.0 已发布**
FIN-001~013 全部 DONE，§20.5 完工判定七项全勾。release commit `3a43ca5`（文档勾选 + 状态改 DONE），tag `v1.0.0` 已推远端。CI run 35066318081 全绿（修复 mypy + bootstrap 后），12fe561..3a43ca5 已推；后续演示脚本已由 commit `2c8c2ac` 纳入仓库。

**本日收口的关键修复**：①bootstrap 幂等化——`ensure_bootstrap_user`（bootstrap.py）对「已存在且凭据匹配+活跃」账号返回 exists 跳过，凭据不匹配才报错；解决 seed 已建 `hr.demo` 与 start_stack bootstrap 撞名导致 one-command 探针 exit 1；②`ONE_COMMAND_UP_VALIDATION_OK` / `API_RUNTIME_VALIDATION_OK` 首次本机跑通；③Secret 扫描零命中（三个含 PAT 残留的临时文件已清空）；④npm/pip 依赖审计干净。

**CI e2e 全红的根因已全部定位并修复**（三层，详见 `PITFALLS.md`）：① 异步连接池被 SSE 流耗尽（`QueuePool limit of size 5 overflow 10 reached`，全池 15 条；SSE 把池化连接持到流结束，池满后同进程每个请求都要等 30s 再 500）；② `SseService.stream` 的 `except Exception` 把基础设施故障误报成 `SSE_AUTH_REVOKED`，顺带补上 **404 撤权门禁**（「无权可见」是用 404 表达的）与 `RunTimeline` 撤权原因展示；③ **`classifySequence` 把「无游标」（`lastAccepted = -1`）下的首帧误判成跳号** —— 服务端 `agent_events.sequence` 是 **1 起**，`1 > -1 + 1` 恒成立，于是每次订阅都在接受任何事件前自断重连、无游标重放同一帧、死循环（时间线永远 0 行）。已在 `apps/web/src/api/sse.ts` 修（`lastAccepted >= 0 &&` 前置），`sse.test.ts` 补 3 条回归且 fixture 全部改 1 起。

## 架构约束（不可妥协）
1. **单 Agent 双运行图**：MatchRun = 岗位级批量分析（不审批、无副作用）；ApplicationRun = 单候选人固定 8 节点双审批图（`human_review` 主中断 + `wait_schedule_approval` 条件中断）。隔离靠 JobApplication 活动 Run 排他槽 + Approval 幂等键。
2. 副作用节点只埋 typed state；真正写表由 SideEffect 服务经 Approval 状态机 + 乐观锁 CAS + `idempotency_key` 保证**只执行一次**。
3. RBAC 服务端每次重校验；SSE 每批/心跳持续授权。
4. MVP 边界：电子 PDF/DOCX（无 OCR）、合成数据、单机、MockSchedule、精确向量检索（HNSW 不默认）、Langfuse 可选。
5. 不可削减项：证据归属/摘录/语义支持、JobAssignment 资源级授权 + SSE 持续授权、活动槽/Approval/幂等/副作用隔离、Checkpoint 恢复 + 协作取消 + Approval 过期、Prompt Injection 配对回归 + 核心自动化测试。

## 文档地图
需求分析.md、概要设计说明书.md、详细设计说明书.md、技术栈选型与架构决策.md、编码实现计划.md（IMP-001~030 + 周门禁 G1~G6；§19.2 诚实 PARTIAL 域、§20.2 FIN 总表、§20.3 分项验收清单、§20.5 完工判定）、UML规划文档.md、项目可行性分析.md、环境配置清单.md、README.md、`docs/adr/0001-agent-runtime-custom-engine-over-langgraph.md`。

## 约定
每次改动配独立 Git commit（AGENTS.md 硬要求）+ 对应测试，交付前确保测试与门禁全通过。（「宋体四号 / 1.5 倍行距 / 字数区间」只适用于学校 SRS 文档，与本项目无关。）
