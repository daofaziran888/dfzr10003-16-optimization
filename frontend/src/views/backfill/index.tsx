import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchJson, postAction } from '@/api/client'
import { operatorHeaders, useSession } from '@/stores/session'

const ENDPOINT = '/api/backfill'
const columns = [
  "批次编号",
  "目标表名",
  "开始日期",
  "结束日期",
  "并行数量",
  "覆盖模式",
  "重跑状态",
  "异常区间"
]
const actions = [
  "启动重跑",
  "暂停重跑",
  "归档批次"
]
const statuses = [
  "草稿",
  "运行中",
  "已暂停",
  "已完成"
]

type Hold = {
  快照编号: string | null
  冻结标记: string
  结论: string
  占用批次: {
    批次编号: string
    目标表名: string
    开始日期: string
    结束日期: string
    重跑状态: string
    引用状态: string
    释放标记: string
    判定说明: string
  }[]
  不能释放原因: string[]
}

type Row = Record<string, string | number | null> & { id: number; 引用快照编号?: string; 引用状态?: string; 释放标记?: string; 释放结论?: string; 快照冻结标记?: Hold }

const HOLD_STYLE: Record<string, string> = {
  可释放: 'releasable',
  已释放: 'released',
  冻结: 'frozen',
  待验证: 'pending',
  未到期: 'notdue',
  未知快照: 'frozen',
  未绑定快照: 'plain',
}

/** 与快照页同一冻结标记：可释放/已释放说明快照已被清理计划收口，不能继续回滚。 */
function rollbackBlocked(hold?: Hold): boolean {
  return hold?.冻结标记 === '可释放' || hold?.冻结标记 === '已释放'
}

export default function BackfillView() {
  const { operator, canCleanup } = useSession()
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])
  const [detail, setDetail] = useState<Row | null>(null)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${query}`, {
        operatorHeaders: operatorHeaders(operator),
      })
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setDetail((prev) => (prev ? (payload.items ?? []).find((row) => row.id === prev.id) ?? prev : prev))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '历史重跑列表读取失败')
    }
  }, [filters, operator])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    const hold = row.快照冻结标记
    if (action === '启动重跑' && rollbackBlocked(hold)) {
      setErrorMessage(`快照 ${hold?.快照编号 ?? ''} 标记为「${hold?.冻结标记}」，不能继续回滚`)
      return
    }
    try {
      const result = await postAction<Row>(`${ENDPOINT}/${row.id}/actions`, { action }, operatorHeaders(operator))
      if (!result.ok) {
        setErrorMessage(result.message)
        return
      }
      setNotice(result.message)
      await reload()
      if (detail?.id === row.id && result.entry) {
        setDetail(result.entry as Row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '历史重跑操作失败')
    }
  }

  const frozenCount = rows.filter((row) => row.快照冻结标记?.冻结标记 === '冻结').length
  const stats = [
    { label: '重跑批次总数', value: rows.length },
    { label: '被快照冻结标记拦下的批次', value: frozenCount },
    { label: '引用快照已释放的批次', value: rows.filter((row) => row.快照冻结标记?.冻结标记 === '已释放').length },
  ]

  return (
    <section className="page" data-module="backfill">
      <header className="page-head">
        <div>
          <h2>历史重跑管理</h2>
          <p className="page-desc">
            维护历史重跑批次的日期范围、并行参数与状态追踪；每条批次读取快照页的同一冻结标记，快照一旦进入可释放/已释放，就不再允许从重跑侧继续回滚。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('重跑批次登记入口尚未接入审批流')}>
            登记重跑批次
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出历史重跑清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

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
            <th>引用快照冻结标记</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const hold = row.快照冻结标记
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td className="reason-cell">
                  {hold ? (
                    <div>
                      <span className={`badge ${HOLD_STYLE[hold.冻结标记] ?? 'plain'}`}>{hold.冻结标记}</span>
                      <div className="hold-cell">
                        <span>快照 {hold.快照编号 ?? '—'}：{hold.结论}</span>
                        {hold.占用批次.length > 0 && <span>占用批次：{hold.占用批次.map((item) => item.批次编号).join('、')}</span>}
                      </div>
                      <button className="link" type="button" onClick={() => setDetail(row)}>查看引用详情</button>
                    </div>
                  ) : (
                    <span className="readonly-hint">—</span>
                  )}
                </td>
                <td className="row-actions">
                  {actions.map((action) => {
                    const blockedByHold = action === '启动重跑' && rollbackBlocked(hold)
                    const blockedByRole = action === '启动重跑' && !canCleanup
                    return (
                      <button
                        key={action}
                        className="link"
                        type="button"
                        disabled={blockedByHold || blockedByRole}
                        title={
                          blockedByHold
                            ? `快照已标记为「${hold?.冻结标记}」，不能继续回滚`
                            : blockedByRole
                              ? '仅数据版本管理员可继续回滚'
                              : undefined
                        }
                        onClick={() => void runAction(action, row)}
                      >
                        {action}
                      </button>
                    )
                  })}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无历史重跑数据，可先登记重跑批次</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <div className="plan-section">
          <h3>引用详情：{String(detail.批次编号)} / 快照 {detail.引用快照编号 ?? '未绑定'}</h3>
          <p className="section-desc">以下冻结标记与快照页使用同一判定口径，不另存副本。</p>
          <table className="sub-table">
            <thead>
              <tr><th>批次编号</th><th>目标表名</th><th>重跑区间</th><th>重跑状态</th><th>引用状态</th><th>释放标记</th><th>判定说明</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>{String(detail.批次编号)}</td>
                <td>{String(detail.目标表名 ?? '—')}</td>
                <td>{String(detail.开始日期 ?? '—')} ~ {String(detail.结束日期 ?? '—')}</td>
                <td>{String(detail.重跑状态 ?? '—')}</td>
                <td>{String(detail.引用状态 ?? '—')}</td>
                <td>{String(detail.释放标记 ?? '—')}</td>
                <td className="reason-cell">
                  <ReasonList reasons={detail.快照冻结标记?.不能释放原因 ?? []} />
                </td>
              </tr>
            </tbody>
          </table>
          {detail.快照冻结标记?.占用批次.map((batch) => (
            <p key={batch.批次编号} className="readonly-hint">
              {batch.批次编号}：{batch.判定说明 || batch.引用状态}
            </p>
          ))}
          {detail.快照冻结标记?.冻结标记 === '冻结' && (
            <div className="notice info">该批次的快照仍被活跃补数占用，快照页会显示为冻结，不会出现在释放窗口。</div>
          )}
          {rollbackBlocked(detail.快照冻结标记) && (
            <div className="notice error">快照已标记为「{detail.快照冻结标记?.冻结标记}」，历史重跑不能再继续回滚该范围。</div>
          )}
        </div>
      )}

      {(errorMessage || notice) && (
        <div className={errorMessage ? 'notice error' : 'notice success'}>{errorMessage || notice}</div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条历史重跑记录</span>
        {(errorMessage || notice) && <span className={errorMessage ? 'error-text' : ''}>{errorMessage || notice}</span>}
      </footer>
    </section>
  )
}

function ReasonList({ reasons }: { reasons: string[] }) {
  if (!reasons.length) return <span className="readonly-hint">—</span>
  return (
    <ul className="reason-list">
      {reasons.map((reason) => <li key={reason}>{reason}</li>)}
    </ul>
  )
}
