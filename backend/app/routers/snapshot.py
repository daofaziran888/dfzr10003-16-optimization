"""快照管理接口：维护快照记录与只读释放计划，清理动作只授予数据版本管理员。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.security import require_cleanup_permission, resolve_actor
from app.services.release import release_service
from app.services.snapshot import CLEANUP_ACTIONS, SnapshotService

router = APIRouter(prefix="/api/snapshot", tags=["快照管理"])

service = SnapshotService()

LIST_FIELDS = ["快照编号", "表名称", "快照时间", "快照大小", "保留天数", "恢复校验", "快照状态", "创建批次"]
STATUSES = ["草稿", "运行中", "已完成", "已清理"]


@router.get("/release-plan", response_model=dict)
def get_release_plan(request: Request) -> dict:
    """只读释放计划：按快照大小、到期时间、恢复校验和活跃补数引用生成三类窗口票。

    该接口不写任何业务记录，任何查看者都可以读取；清理权限另见确认执行接口。
    """
    actor = resolve_actor(request)
    plan = service.plan()
    plan["actor"] = {"role": actor.role, "name": actor.name, "can_cleanup": actor.is_admin}
    return plan


@router.get("/release-plan/executions", response_model=dict)
def list_release_executions() -> dict:
    """清理窗口执行记录（只读）：同一窗口只有一条执行记录，已完成票不重复处理。"""
    return {"items": release_service.executions()}


@router.get("/release-plan/batches", response_model=dict)
def list_cleanup_batches() -> dict:
    """已创建的清理批次，按窗口票幂等生成。"""
    return {"items": release_service.batches()}


@router.post("/release-plan/execute", response_model=ActionResult)
def execute_release_plan(payload: EntryPayload, request: Request) -> ActionResult:
    """确认执行释放计划：按窗口票幂等创建清理批次。

    只有数据版本管理员可以调用；失败窗口停下并保留已完成部分，重试从该窗口继续。
    """
    actor = require_cleanup_permission(request)
    values = payload.merged()
    window_id = str(values.get("window_id") or "").strip()
    if not window_id:
        return ActionResult(ok=False, message="缺少盘点窗口 window_id")
    raw_tickets = values.get("ticket_ids")
    if raw_tickets is None:
        ticket_ids = None
    elif isinstance(raw_tickets, list):
        ticket_ids = [str(item).strip() for item in raw_tickets if str(item).strip()]
    else:
        return ActionResult(ok=False, message="ticket_ids 必须是窗口票编号数组")
    result = release_service.execute(window_id, ticket_ids, actor.name)
    return ActionResult(
        ok=bool(result["ok"]),
        message=str(result["message"]),
        entry=result.get("execution"),
    )


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按快照编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已完成、已清理"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按快照编号与状态过滤快照管理列表，并内嵌占用结论；没有数据时返回空页。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    actor = resolve_actor(request)
    items, total = service.list_with_occupancy(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size, actor=actor.role)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload, request: Request) -> ActionResult:
    """登记一条快照记录，缺字段时说明原因而不是静默丢弃。"""
    require_cleanup_permission(request)
    entry, missing = service.create_entry(payload.merged())
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="快照记录已登记", entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出快照管理清单：返回当前过滤条件下的全量数据（含占用结论）。"""
    items, total = service.list_with_occupancy(page=1, size=10000)
    return {"module": "snapshot", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条快照记录明细与占用结论；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"快照记录 {entry_id} 不存在或已归档")
    return {**entry, "占用结论": service.occupancy(entry)}


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload, request: Request) -> ActionResult:
    """对单条快照记录执行动作；清理/归档动作会先复核占用结论，且仅管理员可执行。"""
    action = str(payload.merged().get("action") or "").strip()
    actor = require_cleanup_permission(request) if action in CLEANUP_ACTIONS else None
    entry, message = service.run_action(entry_id, action, actor.name if actor else "数据版本管理员")
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
