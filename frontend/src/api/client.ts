/** 统一请求封装：拼后端地址、带上当前值班身份、抛网络错误。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type RequestOptions = RequestInit & {
  /** 当前值班身份请求头，写操作由后端再做一次权限复核。 */
  operatorHeaders?: Record<string, string>
}

export function request(path: string, init?: RequestOptions): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  const { operatorHeaders, ...rest } = init ?? {}
  return fetch(url, {
    ...rest,
    headers: {
      'Content-Type': 'application/json',
      ...(operatorHeaders ?? {}),
      ...(rest.headers ?? {}),
    },
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string, init?: RequestOptions): Promise<T> {
  const response = await request(path, init)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

/** 读取动作接口的 { ok, message, entry }，把后端拒绝原因原样带回页面。 */
export async function postAction<T = unknown>(
  path: string,
  body: Record<string, unknown>,
  operatorHeaders: Record<string, string>,
): Promise<{ ok: boolean; message: string; entry?: T }> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
    operatorHeaders,
  })
  if (response.status === 403) {
    const payload = await response.json().catch(() => ({ detail: '没有清理权限' }))
    return { ok: false, message: String(payload.detail ?? '没有清理权限') }
  }
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，动作未生效`)
  }
  return (await response.json()) as { ok: boolean; message: string; entry?: T }
}
