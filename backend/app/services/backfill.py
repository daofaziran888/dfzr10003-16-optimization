"""历史重跑业务规则：状态流转、字段校验、筛选口径与冻结标记复核。"""
from __future__ import annotations

import copy
from typing import Any

from app.services.release import release_hold_for_snapshot
from app.store import store

MODULE = "backfill"
REQUIRED_FIELDS = ["批次编号", "目标表名", "开始日期"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成"]
ACTION_RULES = {"启动重跑": "草稿", "暂停重跑": "运行中", "归档批次": "已暂停"}
NEGATIVE_ACTIONS = []
ROLLBACK_ACTIONS = {"启动重跑"}


class BackfillService:
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
            rows = [row for row in rows if keyword in str(row.get("批次编号", ""))]
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
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"重跑批次 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于历史重跑可执行范围"
        if action in ROLLBACK_ACTIONS:
            # 与快照页同一冻结标记：快照已被判成可释放/已释放时，不允许再从重跑侧回滚。
            hold = self.hold(entry)
            if hold["冻结标记"] in {"可释放", "已释放"}:
                return None, (
                    f"批次 {entry.get('批次编号')} 引用的快照 {hold.get('快照编号')} "
                    f"已标记为「{hold['冻结标记']}」，不能再执行回滚"
                )
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"重跑批次已{action}"

    def hold(self, entry: dict[str, Any]) -> dict[str, Any]:
        """读取快照页的同一冻结标记（只读，不落地）。"""
        return release_hold_for_snapshot(entry.get("引用快照编号"))

    def list_with_holds(self, **filters: Any) -> tuple[list[dict[str, Any]], int]:
        items, total = self.list_entries(**filters)
        enriched = []
        for entry in items:
            row = copy.deepcopy(entry)
            row["快照冻结标记"] = self.hold(entry)
            enriched.append(row)
        return enriched, total
