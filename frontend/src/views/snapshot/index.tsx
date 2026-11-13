import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchJson, postAction } from '@/api/client'
import { useSession, operatorHeaders } from '@/stores/session'

const ENDPOINT = '/api/snapshot'
const columns = [
  "快照编号",
  "表名称",
  "快照时间",
  "快照大小",
  "保留天数",
  "恢复校验",
  "快照状态",
  "创建批次"
]
const actions = [
  "创建快照",
  "清理快照",
  "归档记录"
]
const statuses = [
  "草稿",
  "运行中",
  "已完成",
  "已清理"
]

type Occupancy = {
  release_class: string
  conclusion: string
  occupancy_batches: OccupancyBatch[]
  cannot_release_reasons: string[]
  hold: Hold
}

type OccupancyBatch = {
  批次编号: string
  目标表名: string
  开始日期: string
  结束日期: string
  重跑状态: string
  引用状态: string
  释放标记: string
  判定说明: string
}

type Hold = {
  快照编号: string | null
  冻结标记: string
  结论: string
  占用批次: OccupancyBatch[]
  不能释放原因: string[]
}

type History = { 清理窗口?: string; 结论?: string; 预计回收量?: string; 占用批次?: string[]; 执行记录?: string }

type Row = Record<string, string | number | null> & { id: number; 占用结论?: Occupancy; 历史结果?: History }

type Recovery = { result: string; time: string; evidence: string; missing: boolean }

type Ticket = {
  ticket_id: string
  snapshot_no: string
  table_name: string
  size_text: string
  estimated_reclaim_text: string
  expire_time: string
  release_class: string
  occupancy_batches: OccupancyBatch[]
  cannot_release_reasons: string[]
  recovery: Recovery
  history: History | null
  conclusion: string
  executable: boolean
}

type Plan = {
  window_id: string
  window_label: string
  generated_at: string
  today: string
  summary: Record<string, number | string>
  classes: Record<string, Ticket[]>
  tickets: Ticket[]
  not_due: { snapshot_no: string; table_name: string; expire_time: string; conclusion: string }[]
  released: { snapshot_no: string; table_name: string; size_text: string; conclusion: string; history: History | null }[]
  execution: Execution | null
  cleanup_batches: CleanupBatch[]
  actor?: { role: string; name: string; can_cleanup: boolean }
}

type ExecutionTicket = {
  ticket_id: string
  snapshot_no: string
  release_class: string
  status: string
  message: string
  "清理批次编号": string | null
}

type Execution = {
  window_id: string
  status: string
  attempts: number
  failed_ticket_id: string | null
  tickets: ExecutionTicket[]
  created_at: string
  updated_at: string
}

type CleanupBatch = {
  批次编号: string
  快照编号: string
  表名称: string
  预计回收量: string
  状态: string
  执行人: string
  created_at: string
}

const CLASS_LABEL: Record<string, string> = {
  可释放: 'releasable',
  冻结: 'frozen',
  待验证: 'pending',
  未到期: 'notdue',
  已释放: 'released',
}

function badgeClass(value: string) {
  return `badge ${CLASS_LABEL[value] ?? 'plain'}`
}

function ReasonList({ reasons }: { reasons: string[] }) {
  if (!reasons.length) return <span className="readonly-hint">—</span>
  return (
    <ul className="reason-list">
      {reasons.map((reason) => <li key={reason}>{reason}</li>)}
    </ul>
  )
}

function OccupancyCell({ occupancy }: { occupancy?: Occupancy }) {
  if (!occupancy) return <span className="readonly-hint">—</span>
  return (
    <div>
      <span className={badgeClass(occupancy.release_class)}>{occupancy.release_class}</span>
      <div className="hold-cell">
        <span>{occupancy.conclusion}</span>
        {occupancy.occupancy_batches.length > 0 && (
          <span>占用批次：{occupancy.occupancy_batches.map((batch) => batch.批次编号).join('、')}</span>
        )}
      </div>
    </div>
  )
}

export default function SnapshotView() {
  const { operator, canCleanup } = useSession()
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])
  const [plan, setPlan] = useState<Plan | null>(null)
  const [planLoading, setPlanLoading] = useState(false)
  const [selectedTickets, setSelectedTickets] = useState<Record<string, boolean>>({})
  const [executing, setExecuting] = useState(false)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${query}`, {
        operatorHeaders: operatorHeaders(operator),
      })
      setRows(response.items ?? [])
      setTotal(response.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照管理列表读取失败')
    }
  }, [filters, operator])

  const reloadPlan = useCallback(async () => {
    setPlanLoading(true)
    try {
      const payload = await fetchJson<Plan>(`${ENDPOINT}/release-plan`, {
        operatorHeaders: operatorHeaders(operator),
      })
      setPlan(payload)
      setSelectedTickets((prev) => {
        const next: Record<string, boolean> = {}
        for (const ticket of payload.classes['可释放'] ?? []) {
          next[ticket.ticket_id] = prev[ticket.ticket_id] ?? true
        }
        return next
      })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '释放计划读取失败')
    } finally {
      setPlanLoading(false)
    }
  }, [operator])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadPlan()
  }, [reloadPlan])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const result = await postAction<Row>(`${ENDPOINT}/${row.id}/actions`, { action }, operatorHeaders(operator))
      if (!result.ok) {
        setErrorMessage(result.message)
        return
      }
      setNotice(result.message)
      await Promise.all([reload(), reloadPlan()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照管理操作失败')
    }
  }

  async function executeSelected() {
    if (!plan) return
    const ticketIds = (plan.classes['可释放'] ?? [])
      .filter((ticket) => selectedTickets[ticket.ticket_id])
      .map((ticket) => ticket.ticket_id)
    setErrorMessage('')
    setNotice('')
    if (!ticketIds.length) {
      setErrorMessage('请至少勾选一张可释放窗口票')
      return
    }
    setExecuting(true)
    try {
      const result = await postAction<Execution>(
        `${ENDPOINT}/release-plan/execute`,
        { window_id: plan.window_id, ticket_ids: ticketIds },
        operatorHeaders(operator),
      )
      if (result.ok) {
        setNotice(result.message)
      } else {
        setErrorMessage(result.message)
      }
      await Promise.all([reload(), reloadPlan()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '确认执行失败')
    } finally {
      setExecuting(false)
    }
  }

  const summary = plan?.summary
  const stats = [
    { label: '本窗口预计回收', value: String(summary?.releasable_size_text ?? '—'), hint: `可释放 ${summary?.releasable_count ?? 0} 张窗口票` },
    { label: '冻结占用', value: String(summary?.frozen_size_text ?? '—'), hint: `冻结 ${summary?.frozen_count ?? 0} 张，仍被补数占用` },
    { label: '待验证', value: String(summary?.pending_verification_size_text ?? '—'), hint: `待验证 ${summary?.pending_verification_count ?? 0} 张，恢复校验缺失` },
    { label: '历史已释放', value: String(summary?.released_count ?? 0), hint: '按当时清理窗口留档' },
  ]

  const execution = plan?.execution
  const releasableTickets = plan?.classes['可释放'] ?? []
  const frozenTickets = plan?.classes['冻结'] ?? []
  const pendingTickets = plan?.classes['待验证'] ?? []
  const selectedCount = releasableTickets.filter((ticket) => selectedTickets[ticket.ticket_id]).length

  return (
    <section className="page" data-module="snapshot">
      <header className="page-head">
        <div>
          <h2>快照管理</h2>
          <p className="page-desc">
            维护快照的创建时间、存储控制与恢复校验；下月盘点按到期时间、快照大小、恢复校验和补数活跃引用生成可释放/冻结/待验证窗口票，计划只读，确认执行仅授予数据版本管理员。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('快照记录登记入口尚未接入审批流')}>
            登记快照记录
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出快照管理清单
          </button>
        </div>
      </header>

      {plan && (
        <div className="plan-summary">
          {stats.map((item) => (
            <article key={item.label} className="plan-card">
              <span className="plan-label">{item.label}</span>
              <strong className="plan-value">{item.value}</strong>
              <span className="plan-hint">{item.hint}</span>
            </article>
          ))}
        </div>
      )}

      {plan && (
        <>
          <div className="notice info">
            盘点窗口 {plan.window_label}，生成于 {plan.generated_at}；盘点日 {plan.today}。
            {canCleanup ? '当前身份可确认执行清理。' : '当前身份为只读查看者：可以复核计划，但不能执行清理。'}
          </div>
          {plan.execution && (
            <div className={plan.execution.status === '已完成' ? 'notice success' : 'notice info'}>
              窗口执行记录：{plan.execution.status}，提交 {plan.execution.attempts} 次
              {plan.execution.failed_ticket_id ? `；停在 ${plan.execution.failed_ticket_id}，可从该窗口重试` : ''}
            </div>
          )}
        </>
      )}

      <div className="plan-section">
        <h3>可释放窗口</h3>
        <p className="section-desc">到期、恢复校验通过且无活跃补数占用。勾选后确认执行，只会为勾选的窗口创建清理批次。</p>
        <table className="data-table">
          <thead>
            <tr>
              <th>选择</th><th>窗口票</th><th>快照编号</th><th>表名称</th><th>到期时间</th><th>快照大小</th>
              <th>预计回收量</th><th>恢复校验</th><th>占用批次</th><th>结论</th>
            </tr>
          </thead>
          <tbody>
            {releasableTickets.map((ticket) => (
              <tr key={ticket.ticket_id}>
                <td>
                  <input
                    type="checkbox"
                    checked={selectedTickets[ticket.ticket_id] ?? false}
                    disabled={!canCleanup}
                    onChange={(event) => setSelectedTickets((prev) => ({ ...prev, [ticket.ticket_id]: event.target.checked }))}
                  />
                </td>
                <td>{ticket.ticket_id}</td>
                <td>{ticket.snapshot_no}</td>
                <td>{ticket.table_name}</td>
                <td>{ticket.expire_time}</td>
                <td>{ticket.size_text}</td>
                <td><strong>{ticket.estimated_reclaim_text}</strong></td>
                <td>{ticket.recovery.result}（{ticket.recovery.time}）</td>
                <td>—</td>
                <td>{ticket.conclusion}</td>
              </tr>
            ))}
            {releasableTickets.length === 0 && (
              <tr><td className="empty-state" colSpan={10}>本窗口暂无满足条件的可释放快照</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="plan-section">
        <h3>冻结窗口</h3>
        <p className="section-desc">已到期但被活跃补数批次占用或恢复校验失败。恢复校验缺失与引用状态冲突时，一律按仍被活跃补数占用的一侧处理。</p>
        <table className="data-table">
          <thead>
            <tr>
              <th>窗口票</th><th>快照编号</th><th>快照大小</th><th>到期时间</th><th>恢复校验</th><th>占用批次</th><th>不能释放的原因</th>
            </tr>
          </thead>
          <tbody>
            {frozenTickets.map((ticket) => (
              <tr key={ticket.ticket_id}>
                <td>{ticket.ticket_id}</td>
                <td>{ticket.snapshot_no}</td>
                <td>{ticket.size_text}</td>
                <td>{ticket.expire_time}</td>
                <td>{ticket.recovery.result === '缺失' ? '缺失' : `${ticket.recovery.result}（${ticket.recovery.time}）`}</td>
                <td>{ticket.occupancy_batches.map((batch) => batch.批次编号).join('、') || '—'}</td>
                <td className="reason-cell"><ReasonList reasons={ticket.cannot_release_reasons} /></td>
              </tr>
            ))}
            {frozenTickets.length === 0 && (
              <tr><td className="empty-state" colSpan={7}>本窗口没有冻结快照</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="plan-section">
        <h3>待验证窗口</h3>
        <p className="section-desc">恢复校验证据缺失，既不能释放也不能判定为冻结；补做恢复校验后再复核。</p>
        <table className="data-table">
          <thead>
            <tr>
              <th>窗口票</th><th>快照编号</th><th>快照大小</th><th>到期时间</th><th>恢复校验</th><th>不能释放的原因</th>
            </tr>
          </thead>
          <tbody>
            {pendingTickets.map((ticket) => (
              <tr key={ticket.ticket_id}>
                <td>{ticket.ticket_id}</td>
                <td>{ticket.snapshot_no}</td>
                <td>{ticket.size_text}</td>
                <td>{ticket.expire_time}</td>
                <td>{ticket.recovery.evidence}</td>
                <td className="reason-cell"><ReasonList reasons={ticket.cannot_release_reasons} /></td>
              </tr>
            ))}
            {pendingTickets.length === 0 && (
              <tr><td className="empty-state" colSpan={6}>本窗口没有待验证快照</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="execute-bar">
        <button className="btn primary" type="button" disabled={!canCleanup || executing || selectedCount === 0} onClick={() => void executeSelected()}>
          {executing ? '正在执行…' : `确认执行（${selectedCount} 张可释放窗口票）`}
        </button>
        <button className="btn" type="button" disabled={planLoading} onClick={() => void reloadPlan()}>
          {planLoading ? '刷新中…' : '刷新释放计划'}
        </button>
        <span className="readonly-hint">
          {canCleanup
            ? '执行时按窗口票幂等创建清理批次；失败窗口停下并保留已完成部分。'
            : '只读查看者只能复核计划，清理按钮已禁用。'}
        </span>
      </div>

      {execution && (
        <div className="plan-section">
          <h3>本窗口执行记录</h3>
          <table className="sub-table">
            <thead>
              <tr><th>窗口票</th><th>快照编号</th><th>分类</th><th>执行状态</th><th>清理批次</th><th>说明</th></tr>
            </thead>
            <tbody>
              {execution.tickets.map((ticket) => (
                <tr key={ticket.ticket_id}>
                  <td>{ticket.ticket_id}</td>
                  <td>{ticket.snapshot_no}</td>
                  <td><span className={badgeClass(ticket.release_class)}>{ticket.release_class}</span></td>
                  <td>{ticket.status}</td>
                  <td>{ticket['清理批次编号'] ?? '—'}</td>
                  <td className="reason-cell">{ticket.message || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(plan?.cleanup_batches.length ?? 0) > 0 && (
        <div className="plan-section">
          <h3>已创建的清理批次</h3>
          <table className="sub-table">
            <thead>
              <tr><th>批次编号</th><th>快照编号</th><th>表名称</th><th>预计回收量</th><th>状态</th><th>执行人</th><th>创建时间</th></tr>
            </thead>
            <tbody>
              {plan?.cleanup_batches.map((batch) => (
                <tr key={batch.批次编号}>
                  <td>{batch.批次编号}</td>
                  <td>{batch.快照编号}</td>
                  <td>{batch.表名称}</td>
                  <td>{batch.预计回收量}</td>
                  <td>{batch.状态}</td>
                  <td>{batch.执行人}</td>
                  <td>{batch.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="plan-section">
        <h3>已释放与未到期快照</h3>
        <table className="sub-table">
          <thead>
            <tr><th>快照编号</th><th>表名称</th><th>快照大小</th><th>到期时间</th><th>结论</th><th>历史结果</th></tr>
          </thead>
          <tbody>
            {(plan?.released ?? []).map((item) => (
              <tr key={item.snapshot_no}>
                <td>{item.snapshot_no}</td>
                <td>{item.table_name}</td>
                <td>{item.size_text}</td>
                <td>—</td>
                <td><span className="badge released">已释放</span> {item.conclusion}</td>
                <td>{item.history ? `${item.history.清理窗口} 窗口 / ${item.history.结论} / ${item.history.执行记录}` : '—'}</td>
              </tr>
            ))}
            {(plan?.not_due ?? []).map((item) => (
              <tr key={item.snapshot_no}>
                <td>{item.snapshot_no}</td>
                <td>{item.table_name}</td>
                <td>—</td>
                <td>{item.expire_time}</td>
                <td><span className="badge notdue">未到期</span> {item.conclusion}</td>
                <td>—</td>
              </tr>
            ))}
            {(plan?.released.length ?? 0) === 0 && (plan?.not_due.length ?? 0) === 0 && (
              <tr><td className="empty-state" colSpan={6}>暂无已释放或未到期快照</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {(errorMessage || notice) && (
        <div className={errorMessage ? 'notice error' : 'notice success'}>{errorMessage || notice}</div>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>占用结论</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="reason-cell">
                <OccupancyCell occupancy={row.占用结论} />
                {row.历史结果 && (
                  <div className="readonly-hint">历史：{row.历史结果.清理窗口} 窗口 / {row.历史结果.结论}</div>
                )}
              </td>
              <td className="row-actions">
                {actions.map((action) => {
                  const isCleanup = action === '清理快照' || action === '归档记录'
                  const blocked = isCleanup && !canCleanup
                  return (
                    <button
                      key={action}
                      className="link"
                      type="button"
                      disabled={blocked}
                      title={blocked ? '仅数据版本管理员可执行清理' : undefined}
                      onClick={() => void runAction(action, row)}
                    >
                      {action}
                    </button>
                  )
                })}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无快照管理数据，可先登记快照记录</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条快照管理记录</span>
        {(errorMessage || notice) && <span className={errorMessage ? 'error-text' : ''}>{errorMessage || notice}</span>}
      </footer>
    </section>
  )
}
