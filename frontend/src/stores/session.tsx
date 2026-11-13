/** 会话与身份状态：快照清理权限只授予数据版本管理员，其他角色只读。 */
import { createContext, useContext, useMemo, useState } from 'react'

export type OperatorRole = 'admin' | 'viewer'

export type Operator = {
  role: OperatorRole
  name: string
}

export const ROLE_LABELS: Record<OperatorRole, string> = {
  admin: '数据版本管理员',
  viewer: '查看者',
}

const STORAGE_KEY = 'dw.operator'

const defaultOperator: Operator = { role: 'admin', name: '值班数据版本管理员' }

function readStored(): Operator {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return defaultOperator
    const parsed = JSON.parse(raw) as Partial<Operator>
    const role: OperatorRole = parsed.role === 'viewer' ? 'viewer' : 'admin'
    return { role, name: parsed.name?.trim() || ROLE_LABELS[role] }
  } catch {
    return defaultOperator
  }
}

export function operatorHeaders(operator: Operator): Record<string, string> {
  // 请求头只能是 ASCII，中文值班名按 RFC 8187 转义，后端会解码展示。
  return {
    'X-Operator-Role': operator.role,
    'X-Operator-Name': encodeURIComponent(operator.name),
  }
}

type SessionContextValue = {
  operator: Operator
  /** 当前身份是否可以执行清理 / 回滚等写操作。 */
  canCleanup: boolean
  switchRole: (role: OperatorRole, name?: string) => void
}

const SessionContext = createContext<SessionContextValue | null>(null)

export function createSessionValue(
  operator: Operator,
  setOperator: (next: Operator) => void,
): SessionContextValue {
  return {
    operator,
    canCleanup: operator.role === 'admin',
    switchRole: (role, name) => {
      const next = { role, name: name?.trim() || ROLE_LABELS[role] }
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
      } catch {
        // 本地存储不可用时仅保留当前会话内的身份切换。
      }
      setOperator(next)
    },
  }
}

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [operator, setOperator] = useState<Operator>(readStored)
  const value = useMemo(() => createSessionValue(operator, setOperator), [operator])
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext)
  if (!value) {
    throw new Error('useSession 必须在 SessionProvider 内使用')
  }
  return value
}
