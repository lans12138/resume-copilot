import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { api } from "../api/client"
import type { EvaluationDetail, EvaluationRunSummary, MetricSnapshotView } from "../api/types"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import {
  evaluationKindLabel,
  evaluationStatusLabel,
  evaluationVerdictLabel,
  evaluationVerdictTone,
  formatMetricValue,
  formatThreshold,
  isTerminalEvaluationStatus,
  metricLabel,
} from "../lib/evaluationStatus"
import { useAppStore } from "../state/session"

/**
 * Evaluation results page (FIN-007, detailed design §12.6).
 *
 * This is the read side of the offline gate: a list of runs and, for a selected
 * run, the per-metric table. Three deliberate choices:
 *
 * * **The verdict is never recomputed here.** `passed` and `threshold` come from
 *   the stored `metric_snapshots`, so the page, the API and the CI gate cannot
 *   disagree. A metric with no threshold is rendered as informational rather than
 *   as a pass, because reporting an unthresholded number as green would credit the
 *   gate with a check it never made.
 * * **The failing metrics are listed first.** The question a reader arrives with is
 *   "what broke", not "what is the recall number" — so `failed` rows sort to the
 *   top and a summary line states the count. A green run keeps its natural metric
 *   order so a passing number stays findable.
 * * **A run with no verdict is distinguished from a failing one.** `passed === null`
 *   means the run has not finished; rendering that as a failure would make every
 *   in-flight evaluation look like a regression.
 */
export function EvaluationsPage() {
  const token = useAppStore((state) => state.accessToken)!
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const evaluations = useQuery({
    queryKey: ["evaluations"],
    queryFn: () => api.listEvaluations(token, { pageSize: 50 }),
  })

  const detail = useQuery({
    queryKey: ["evaluation", selectedId],
    queryFn: () => api.getEvaluation(token, selectedId!),
    enabled: selectedId !== null,
  })

  if (evaluations.isLoading) return <LoadingState label="正在加载评测记录" />
  if (evaluations.isError) return <ErrorNotice error={evaluations.error} />

  const runs = evaluations.data?.items ?? []

  return (
    <section>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Offline gate</p>
          <h1>评测结果</h1>
          <p className="muted">
            离线门禁记录：检索 Golden、语义支持与注入防护三套评测的指标、阈值与结论。
            每个阈值都会单独留痕，未达标时可以直接看到是哪一项、差多少。
          </p>
        </div>
      </div>

      {runs.length === 0 ? (
        <div className="panel">
          <p className="muted">还没有评测记录。提交评测后，结果会出现在这里。</p>
        </div>
      ) : (
        <div className="panel">
          <div className="section-heading">
            <h2>评测历史</h2>
            <span>共 {evaluations.data?.total ?? 0} 条</span>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">类型</th>
                <th scope="col">状态</th>
                <th scope="col">结论</th>
                <th scope="col">未达标项</th>
                <th scope="col">开始时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <EvaluationRow
                  key={run.id}
                  run={run}
                  onSelect={() => setSelectedId(run.id)}
                  selected={run.id === selectedId}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selectedId !== null ? (
        detail.isLoading ? (
          <LoadingState label="正在加载评测详情" />
        ) : detail.isError ? (
          <ErrorNotice error={detail.error} />
        ) : detail.data ? (
          <EvaluationDetailPanel detail={detail.data} />
        ) : null
      ) : null}
    </section>
  )
}

function EvaluationRow({
  run,
  selected,
  onSelect,
}: {
  run: EvaluationRunSummary
  selected: boolean
  onSelect: () => void
}) {
  const tone = evaluationVerdictTone(run.passed, run.status)
  return (
    <tr className={selected ? "row-selected" : undefined}>
      <td>{evaluationKindLabel(run.kind)}</td>
      <td>{evaluationStatusLabel(run.status)}</td>
      <td>
        <span className={`verdict verdict-${tone}`}>{evaluationVerdictLabel(run.passed, run.status)}</span>
      </td>
      <td>
        {/* Zero is shown as "—" rather than "0": an empty count is not a datum a
            reader should have to parse. A non-zero count is the whole signal. */}
        {run.failing_metric_count > 0 ? run.failing_metric_count : "—"}
      </td>
      <td>{formatTimestamp(run.started_at ?? run.created_at)}</td>
      <td>
        <button className="button button-ghost button-small" type="button" onClick={onSelect}>
          查看指标
        </button>
      </td>
    </tr>
  )
}

function EvaluationDetailPanel({ detail }: { detail: EvaluationDetail }) {
  const metrics = sortMetrics(detail.metrics)
  const failing = metrics.filter((metric) => metric.threshold !== null && !metric.passed)
  const run = detail.run

  return (
    <div className="panel">
      <div className="section-heading">
        <h2>{evaluationKindLabel(run.kind)} · 指标明细</h2>
        <span>
          数据集 {detail.dataset.name}@{detail.dataset.version}
        </span>
      </div>

      <dl className="detail-grid">
        <div>
          <dt>状态</dt>
          <dd>{evaluationStatusLabel(run.status)}</dd>
        </div>
        <div>
          <dt>结论</dt>
          <dd>
            <span className={`verdict verdict-${evaluationVerdictTone(run.passed, run.status)}`}>
              {evaluationVerdictLabel(run.passed, run.status)}
            </span>
          </dd>
        </div>
        <div>
          <dt>配置指纹</dt>
          <dd className="mono">{run.config_hash.slice(0, 12)}…</dd>
        </div>
        <div>
          <dt>数据集内容指纹</dt>
          <dd className="mono">{detail.dataset.content_hash.slice(0, 12)}…</dd>
        </div>
        <div>
          <dt>完成时间</dt>
          <dd>{formatTimestamp(run.finished_at)}</dd>
        </div>
      </dl>

      {run.status === "FAILED" ? (
        <div className="notice notice-error" role="alert">
          <strong>评测执行失败</strong>
          {detail.error_message_safe ? <span>{detail.error_message_safe}</span> : null}
          {run.error_code ? <small>错误码：{run.error_code}</small> : null}
        </div>
      ) : null}

      {failing.length > 0 ? (
        <p className="notice notice-warning" role="status">
          有 {failing.length} 项未达标：{failing.map((metric) => metricLabel(metric.metric_name)).join("、")}
        </p>
      ) : null}

      {metrics.length === 0 ? (
        <p className="muted">
          {isTerminalEvaluationStatus(run.status) ? "这次评测没有产生任何指标。" : "指标仍在计算中。"}
        </p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">指标</th>
              <th scope="col">数值</th>
              <th scope="col">阈值</th>
              <th scope="col">结果</th>
            </tr>
          </thead>
          <tbody>
            {metrics.map((metric) => (
              <MetricRow key={metric.metric_name} metric={metric} />
            ))}
          </tbody>
        </table>
      )}

      <p className="muted">
        评测记录一旦完成就不再修改；重新评测会生成新的一条记录。返回
        <Link to="/jobs"> 岗位工作台</Link>。
      </p>
    </div>
  )
}

function MetricRow({ metric }: { metric: MetricSnapshotView }) {
  const informational = metric.threshold === null
  return (
    <tr>
      <td>{metricLabel(metric.metric_name)}</td>
      <td className="mono">{formatMetricValue(metric.metric_value, metric.metric_name)}</td>
      <td className="mono">{formatThreshold(metric.threshold, metric.metric_name)}</td>
      <td>
        {/* An unthresholded metric is "参考" — it did not participate in the gate,
            and showing it as a pass would credit the gate with a check it skipped. */}
        {informational ? (
          <span className="verdict verdict-pending">参考</span>
        ) : (
          <span className={`verdict verdict-${metric.passed ? "pass" : "fail"}`}>
            {metric.passed ? "通过" : "未达标"}
          </span>
        )}
      </td>
    </tr>
  )
}

/**
 * Sort metrics so a reader sees the problem first.
 *
 * Failing rows lead, then informational rows last (they carry no verdict), then the
 * rest keep their server order. Sorting a *green* run by anything would scramble a
 * familiar sequence for no benefit, so a passing run is left alone.
 */
function sortMetrics(metrics: MetricSnapshotView[]): MetricSnapshotView[] {
  const failing = metrics.filter((metric) => metric.threshold !== null && !metric.passed)
  if (failing.length === 0) return metrics
  const passing = metrics.filter((metric) => metric.threshold !== null && metric.passed)
  const informational = metrics.filter((metric) => metric.threshold === null)
  return [...failing, ...passing, ...informational]
}

function formatTimestamp(value: string | null): string {
  if (!value) return "—"
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString("zh-CN", { hour12: false })
}
