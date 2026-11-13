"""历史重跑接口：维护重跑批次，回滚前读取快照页的同一冻结标记。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.security import require_cleanup_permission, resolve_actor
from app.services.backfill import BackfillService

router = APIRouter(prefix="/api/backfill", tags=["历史重跑"])

service = BackfillService()

LIST_FIELDS = ["批次编号", "目标表名", "开始日期", "结束日期", "并行数量", "覆盖模式", "重跑状态", "异常区间"]
STATUSES = ["草稿", "运行中", "已暂停", "已完成"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按批次编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按批次编号与状态过滤历史重跑列表，并内嵌引用的快照冻结标记。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    actor = resolve_actor(request)
    items, total = service.list_with_holds(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size, actor=actor.role)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条重跑批次明细与快照冻结标记；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重跑批次 {entry_id} 不存在或已归档")
    return {**entry, "快照冻结标记": service.hold(entry)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload, request: Request) -> ActionResult:
    """登记一条重跑批次，缺字段时说明原因而不是静默丢弃。"""
    require_cleanup_permission(request)
    entry, missing = service.create_entry(payload.merged())
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="重跑批次已登记", entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出历史重跑清单：返回当前过滤条件下的全量数据（含冻结标记）。"""
    items, total = service.list_with_holds(page=1, size=10000)
    return {"module": "backfill", "total": total, "items": items}


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload, request: Request) -> ActionResult:
    """执行启动重跑、暂停重跑、归档批次；回滚前先看快照冻结标记。"""
    action = str(payload.merged().get("action") or "").strip()
    if action == "启动重跑":
        # 启动重跑会继续回滚历史数据，必须与快照清理权限同一准入口径。
        require_cleanup_permission(request)
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
