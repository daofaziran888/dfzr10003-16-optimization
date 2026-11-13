"""角色判定：盘点计划默认只读，清理动作只授予数据版本管理员。

当前仓库没有接入统一登录，示例环境用请求头承载当前值班身份：
- X-Operator-Role: admin（数据版本管理员）/ viewer（查看者）
- X-Operator-Name: 值班人显示名（可选）

请求头按 HTTP 规范只能是 ASCII，所以这里用 admin/viewer 作为角色代码，
展示名再映射成中文。生产环境应换成登录态解析；所有写接口仍会在这里做一次
服务端复核，不能只依赖前端隐藏按钮。
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote

from fastapi import HTTPException, Request

ROLE_ADMIN = "数据版本管理员"
ROLE_VIEWER = "查看者"

_ROLE_CODES = {
    "admin": ROLE_ADMIN,
    "数据版本管理员": ROLE_ADMIN,
    "viewer": ROLE_VIEWER,
    "查看者": ROLE_VIEWER,
}


@dataclass(frozen=True)
class Actor:
    role: str
    name: str

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def resolve_actor(request: Request) -> Actor:
    """读取当前值班身份；未声明或角色不可识别时一律降级为只读查看者。"""
    raw_role = str(request.headers.get("X-Operator-Role") or "").strip()
    # 非 ASCII 请求头按 RFC 8187 会以 percent-encoding 传输，这里同时兼容。
    role = _ROLE_CODES.get(unquote(raw_role), _ROLE_CODES.get(raw_role, ROLE_VIEWER))
    default_name = ROLE_ADMIN if role == ROLE_ADMIN else "只读查看者"
    raw_name = unquote(str(request.headers.get("X-Operator-Name") or "").strip())
    name = raw_name or default_name
    return Actor(role=role, name=name)


def require_cleanup_permission(request: Request) -> Actor:
    """清理/变更类接口的准入检查：非数据版本管理员直接 403。"""
    actor = resolve_actor(request)
    if not actor.is_admin:
        raise HTTPException(
            status_code=403,
            detail="当前身份为只读查看者：释放计划与快照清理仅授予数据版本管理员",
        )
    return actor
