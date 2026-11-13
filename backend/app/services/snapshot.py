"""快照管理业务规则：状态流转、字段校验、筛选口径与释放盘点都收在这里。"""
from __future__ import annotations

import copy
from typing import Any

from app.services.release import manual_release, release_hold_for_snapshot, release_service
from app.store import store

MODULE = "snapshot"
REQUIRED_FIELDS = ["快照编号", "表名称", "快照时间"]
STATUS_ORDER = ["草稿", "运行中", "已完成", "已清理"]
ACTION_RULES = {"创建快照": "草稿", "清理快照": "已完成", "归档记录": "已清理"}
NEGATIVE_ACTIONS = []
CLEANUP_ACTIONS = {"清理快照", "归档记录"}


class SnapshotService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("快照编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["冻结标记"] = None
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str, operator: str = "数据版本管理员") -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"快照记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于快照管理可执行范围"
        if action in CLEANUP_ACTIONS:
            # 清理/归档仍然走释放计划的同一判定口径，不能绕过占用结论直接改状态；
            # 状态与释放标记由清理批次统一落库，这里不再单独覆盖。
            released, message = manual_release(str(entry.get("快照编号") or ""), operator)
            if not released:
                return None, message
            return store.find(MODULE, entry_id), message
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"快照记录已{action}"

    def plan(self) -> dict[str, Any]:
        """快照页的释放计划：只读，不修改任何快照记录。"""
        return release_service.plan()

    def occupancy(self, entry: dict[str, Any]) -> dict[str, Any]:
        """单条快照的占用结论，供列表页内嵌展示（与历史重跑页同一冻结标记）。"""
        hold = release_hold_for_snapshot(str(entry.get("快照编号") or ""))
        return {
            "release_class": hold["冻结标记"],
            "conclusion": hold["结论"],
            "occupancy_batches": hold["占用批次"],
            "cannot_release_reasons": hold["不能释放原因"],
            "hold": hold,
        }

    def list_with_occupancy(self, **filters: Any) -> tuple[list[dict[str, Any]], int]:
        items, total = self.list_entries(**filters)
        enriched = []
        for entry in items:
            row = copy.deepcopy(entry)
            row["占用结论"] = self.occupancy(entry)
            enriched.append(row)
        return enriched, total
