"""快照释放计划：从「快照到期」一路判定到「补数引用是否仍然活跃」。

这是快照页与历史重跑页共用的唯一判定口径，避免一边把快照标成可清理、一边
历史重跑还在继续回滚。判定结果只读，不修改任何业务记录；真正落库的只有
数据版本管理员确认执行后的清理批次与执行记录。

判定顺序（后面的分支不会覆盖前面的结论）：
1. 已释放/已清理的快照不再进入盘点；
2. 未到期快照不进入盘点；
3. 仍被活跃补数批次引用 -> 冻结（即使恢复校验缺失或冲突，也以活跃占用一侧为准）；
4. 恢复校验明确失败 -> 冻结；
5. 恢复校验明确通过且无活跃占用 -> 可释放；
6. 其余（恢复校验缺失、证据无法判定）-> 待验证。

恢复证据的取舍：以快照记录里的「恢复校验结果」字段作为唯一权威证据，
校验时间与校验批次只作为说明；结果字段缺失就视为缺失，绝不用「快照状态」
或备注文字推断为通过。引用状态的取舍：只要「引用状态」为活跃引用，或
「释放标记」为占用中，任一条件成立就按活跃引用处理；两侧互相矛盾时也按
活跃引用一侧处理，宁可多冻结一个快照，也不误删仍被补数使用的数据。
"""
from __future__ import annotations

import copy
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

SNAPSHOT_MODULE = "snapshot"
BACKFILL_MODULE = "backfill"
CLEANUP_TABLE = "cleanup_batch"
EXECUTION_TABLE = "release_execution"

CLASS_RELEASABLE = "可释放"
CLASS_FROZEN = "冻结"
CLASS_PENDING = "待验证"
CLASS_NOT_DUE = "未到期"
CLASS_ORDER = [CLASS_RELEASABLE, CLASS_FROZEN, CLASS_PENDING]

# 同一窗口的确认执行必须串行；真实数据库里对应 (window_id) 唯一执行记录
# 与 (window_id, ticket_id) 唯一清理批次，这里用进程内锁复现同样的语义。
_EXECUTION_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> date:
    return date.today()


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_size_gb(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(value or ""))
    return float(match.group(1)) if match else 0.0


def _size_text(size_gb: float) -> str:
    return f"{size_gb:.1f} GB"


def _format_date(value: date | None) -> str:
    return value.isoformat() if value else "—"


def current_window(today: date | None = None) -> str:
    """下个月的盘点窗口编号，例如 2026-12。"""
    base = today or _today()
    year, month = base.year, base.month + 1
    if month > 12:
        year, month = year + 1, 1
    return f"{year:04d}-{month:02d}"


def window_label(window_id: str) -> str:
    return f"{window_id} 快照空间盘点（下月释放窗口）"


def ticket_id_for(window_id: str, snapshot_no: str) -> str:
    return f"WIN-{window_id}-{snapshot_no}"


def cleanup_batch_id(window_id: str, snapshot_no: str) -> str:
    return f"CLEAN-{window_id}-{snapshot_no}"


def _is_released(snapshot: dict[str, Any]) -> bool:
    return (
        str(snapshot.get("冻结标记") or "") == "已释放"
        or str(snapshot.get("快照状态") or "") == "已清理"
        or str(snapshot.get("status") or "") == "已清理"
    )


def _recovery(snapshot: dict[str, Any]) -> dict[str, Any]:
    """选取恢复校验证据：只认「恢复校验结果」字段。"""
    result = str(snapshot.get("恢复校验结果") or "").strip()
    if result not in {"通过", "失败"}:
        result = "缺失"
    return {
        "result": result,
        "time": str(snapshot.get("恢复校验时间") or "").strip() or "—",
        "evidence": str(snapshot.get("恢复校验") or "").strip() or "未登记恢复校验记录",
        "missing": result == "缺失",
    }


def _reference_state(backfill_row: dict[str, Any]) -> dict[str, Any]:
    """判定单条补数引用是否仍然活跃；冲突时按活跃一侧处理。"""
    ref_state = str(backfill_row.get("引用状态") or "").strip()
    hold = str(backfill_row.get("释放标记") or "").strip()
    reasons: list[str] = []
    active = False

    if ref_state == "活跃引用":
        active = True
        reasons.append("引用状态为「活跃引用」")
    elif ref_state == "已释放":
        if hold == "占用中":
            active = True
            reasons.append("引用状态为「已释放」但释放标记为「占用中」，按活跃占用一侧处理")
        elif hold not in {"已释放", ""}:
            active = True
            reasons.append(f"释放标记「{hold}」无法识别，按活跃占用一侧处理")
    elif ref_state == "无引用":
        if hold == "占用中":
            active = True
            reasons.append("引用状态为「无引用」但释放标记为「占用中」，按活跃占用一侧处理")
        elif hold not in {"", "已释放"}:
            active = True
            reasons.append(f"释放标记「{hold}」无法识别，按活跃占用一侧处理")
    elif ref_state == "":
        if hold == "占用中":
            active = True
            reasons.append("引用状态缺失但释放标记为「占用中」，按活跃占用一侧处理")
        elif hold not in {"", "已释放"}:
            active = True
            reasons.append(f"引用状态缺失且释放标记「{hold}」无法识别，按活跃占用一侧处理")
    else:
        # 无法识别的引用状态不能当成已释放，保守按活跃占用处理。
        active = True
        reasons.append(f"引用状态「{ref_state}」无法识别，按活跃占用一侧处理")

    if hold == "占用中" and not any("占用中" in reason for reason in reasons):
        reasons.append("释放标记为「占用中」")
    if active and not reasons:
        reasons.append("存在未释放的补数占用")
    return {"active": active, "state": ref_state or "缺失", "hold": hold or "缺失", "reasons": reasons}


def _active_references(snapshot_no: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for row in store.rows(BACKFILL_MODULE):
        if str(row.get("引用快照编号") or "") != snapshot_no:
            continue
        state = _reference_state(row)
        if not state["active"]:
            continue
        refs.append({
            "批次编号": str(row.get("批次编号") or ""),
            "目标表名": str(row.get("目标表名") or "—"),
            "开始日期": str(row.get("开始日期") or "—"),
            "结束日期": str(row.get("结束日期") or "—"),
            "重跑状态": str(row.get("重跑状态") or row.get("status") or "—"),
            "引用状态": state["state"],
            "释放标记": state["hold"],
            "判定说明": "；".join(state["reasons"]),
        })
    return refs


def evaluate_snapshot(snapshot: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """对单个快照给出占用结论、分类和不能释放的原因（不改动记录）。"""
    base = today or _today()
    snapshot_no = str(snapshot.get("快照编号") or "")
    retention_days = int(snapshot.get("保留天数") or 0)
    snapshot_date = _parse_date(snapshot.get("快照时间"))
    expiry = snapshot_date + timedelta(days=retention_days) if snapshot_date else None
    size_gb = _parse_size_gb(snapshot.get("快照大小GB") or snapshot.get("快照大小"))
    recovery = _recovery(snapshot)
    active_refs = _active_references(snapshot_no)
    reasons: list[str] = []

    if _is_released(snapshot):
        status, conclusion = "已释放", "已按清理窗口留档并释放"
    elif expiry is None:
        status, conclusion = CLASS_PENDING, "快照时间或保留天数缺失，无法计算到期时间"
        reasons.append("快照时间或保留天数缺失，无法判定到期窗口")
    elif expiry > base:
        status, conclusion = CLASS_NOT_DUE, f"到期时间 {expiry.isoformat()} 晚于盘点日，未进入本月释放窗口"
    elif active_refs:
        status = CLASS_FROZEN
        for ref in active_refs:
            reasons.append(
                f"仍被补数批次 {ref['批次编号']} 活跃引用"
                f"（重跑状态：{ref['重跑状态']}，区间 {ref['开始日期']} ~ {ref['结束日期']}）"
            )
            if ref["判定说明"] and ref["判定说明"] != "引用状态为「活跃引用」":
                reasons.append(f"{ref['批次编号']}：{ref['判定说明']}")
        if recovery["missing"]:
            reasons.append("恢复校验记录缺失；因存在活跃补数占用，以占用一侧为准，优先冻结")
        elif recovery["result"] == "失败":
            reasons.append(f"恢复校验结果为失败（{recovery['time']}）")
        reasons.append("补数批次未释放前不允许清理快照")
        conclusion = "已到期但仍被活跃补数占用，冻结"
    elif recovery["result"] == "失败":
        status = CLASS_FROZEN
        reasons.append(f"恢复校验结果为失败（校验时间 {recovery['time']}，证据：{recovery['evidence']}）")
        reasons.append("恢复校验失败不允许进入释放批次")
        conclusion = "已到期但恢复校验失败，冻结"
    elif recovery["result"] == "通过":
        status = CLASS_RELEASABLE
        conclusion = "到期且恢复校验通过，无活跃补数占用，可纳入释放计划"
    else:
        status = CLASS_PENDING
        reasons.append("恢复校验记录缺失，未取得可用恢复证据")
        reasons.append("需补做恢复校验后才能转为可释放或冻结")
        conclusion = "已到期但恢复校验缺失，待验证"

    return {
        "snapshot_no": snapshot_no,
        "snapshot_id": snapshot.get("id"),
        "table_name": str(snapshot.get("表名称") or "—"),
        "snapshot_time": _format_date(snapshot_date),
        "expire_time": _format_date(expiry),
        "retention_days": retention_days,
        "size_gb": size_gb,
        "size_text": _size_text(size_gb),
        "recovery": recovery,
        "occupancy_batches": active_refs,
        "release_class": status,
        "conclusion": conclusion,
        "cannot_release_reasons": reasons,
        "released": status == "已释放",
        "history": copy.deepcopy(snapshot.get("历史结果")) or None,
    }


def evaluate_snapshot_no(snapshot_no: str, today: date | None = None) -> dict[str, Any] | None:
    for row in store.rows(SNAPSHOT_MODULE):
        if str(row.get("快照编号") or "") == snapshot_no:
            return evaluate_snapshot(row, today)
    return None


def release_hold_for_snapshot(snapshot_no: str | None) -> dict[str, Any]:
    """历史重跑页读取的同一冻结标记；未知快照按待验证保守处理。"""
    if not snapshot_no:
        return {
            "快照编号": None,
            "冻结标记": "未绑定快照",
            "结论": "该补数批次未绑定快照，不参与快照释放计划",
            "占用批次": [],
            "不能释放原因": [],
        }
    evaluated = evaluate_snapshot_no(str(snapshot_no))
    if evaluated is None:
        return {
            "快照编号": str(snapshot_no),
            "冻结标记": "未知快照",
            "结论": "引用的快照不存在，按占用中保守处理",
            "占用批次": [],
            "不能释放原因": ["引用的快照不存在"],
        }
    marker = {
        CLASS_RELEASABLE: "可释放",
        CLASS_FROZEN: "冻结",
        CLASS_PENDING: "待验证",
        CLASS_NOT_DUE: "未到期",
        "已释放": "已释放",
    }[evaluated["release_class"]]
    return {
        "快照编号": evaluated["snapshot_no"],
        "冻结标记": marker,
        "结论": evaluated["conclusion"],
        "占用批次": evaluated["occupancy_batches"],
        "不能释放原因": evaluated["cannot_release_reasons"],
    }


def _ticket(window_id: str, evaluated: dict[str, Any]) -> dict[str, Any]:
    releasable = evaluated["release_class"] == CLASS_RELEASABLE
    estimate = evaluated["size_gb"] if releasable else 0.0
    return {
        "ticket_id": ticket_id_for(window_id, evaluated["snapshot_no"]),
        "window_id": window_id,
        "release_class": evaluated["release_class"],
        "snapshot_no": evaluated["snapshot_no"],
        "snapshot_id": evaluated["snapshot_id"],
        "table_name": evaluated["table_name"],
        "snapshot_time": evaluated["snapshot_time"],
        "expire_time": evaluated["expire_time"],
        "retention_days": evaluated["retention_days"],
        "size_gb": evaluated["size_gb"],
        "size_text": evaluated["size_text"],
        "estimated_reclaim_gb": round(estimate, 2),
        "estimated_reclaim_text": _size_text(estimate) if releasable else "0.0 GB（当前不可回收）",
        "recovery": evaluated["recovery"],
        "occupancy_batches": evaluated["occupancy_batches"],
        "cannot_release_reasons": evaluated["cannot_release_reasons"],
        "conclusion": evaluated["conclusion"],
        "history": evaluated["history"],
        "executable": releasable,
    }


def build_plan(window_id: str | None = None, today: date | None = None) -> dict[str, Any]:
    """生成只读释放计划；不写任何业务表。"""
    base = today or _today()
    window = window_id or current_window(base)
    tickets: list[dict[str, Any]] = []
    not_due: list[dict[str, Any]] = []
    released: list[dict[str, Any]] = []

    for snapshot in store.rows(SNAPSHOT_MODULE):
        evaluated = evaluate_snapshot(snapshot, base)
        if evaluated["release_class"] == "已释放":
            released.append({
                "snapshot_no": evaluated["snapshot_no"],
                "table_name": evaluated["table_name"],
                "size_text": evaluated["size_text"],
                "conclusion": evaluated["conclusion"],
                "history": evaluated["history"],
            })
            continue
        if evaluated["release_class"] == CLASS_NOT_DUE:
            not_due.append({
                "snapshot_no": evaluated["snapshot_no"],
                "table_name": evaluated["table_name"],
                "expire_time": evaluated["expire_time"],
                "conclusion": evaluated["conclusion"],
            })
            continue
        tickets.append(_ticket(window, evaluated))

    # 窗口票按分类分组输出，组内按快照编号稳定排序，保证重试时顺序可复核。
    tickets.sort(key=lambda item: (CLASS_ORDER.index(item["release_class"]), item["snapshot_no"]))
    classes = {name: [item for item in tickets if item["release_class"] == name] for name in CLASS_ORDER}
    summary = {
        "expired_total": len(tickets),
        "releasable_count": len(classes[CLASS_RELEASABLE]),
        "releasable_size_gb": round(sum(item["size_gb"] for item in classes[CLASS_RELEASABLE]), 2),
        "frozen_count": len(classes[CLASS_FROZEN]),
        "frozen_size_gb": round(sum(item["size_gb"] for item in classes[CLASS_FROZEN]), 2),
        "pending_verification_count": len(classes[CLASS_PENDING]),
        "pending_verification_size_gb": round(sum(item["size_gb"] for item in classes[CLASS_PENDING]), 2),
        "released_count": len(released),
        "can_execute_count": len(classes[CLASS_RELEASABLE]),
    }
    summary["releasable_size_text"] = _size_text(summary["releasable_size_gb"])
    summary["frozen_size_text"] = _size_text(summary["frozen_size_gb"])
    summary["pending_verification_size_text"] = _size_text(summary["pending_verification_size_gb"])

    return {
        "window_id": window,
        "window_label": window_label(window),
        "generated_at": _now(),
        "today": base.isoformat(),
        "read_only": True,
        "summary": summary,
        "classes": classes,
        "tickets": tickets,
        "released": released,
        "not_due": not_due,
        "execution": _find_execution(window),
        "cleanup_batches": _list_batches(window),
    }


def _find_execution(window_id: str) -> dict[str, Any] | None:
    row = store.find_internal(EXECUTION_TABLE, window_id=window_id)
    return copy.deepcopy(row) if row is not None else None


def _list_batches(window_id: str | None = None) -> list[dict[str, Any]]:
    rows = store.internal_rows(CLEANUP_TABLE)
    if window_id:
        rows = [row for row in rows if row.get("window_id") == window_id]
    return copy.deepcopy(rows)


def list_executions() -> list[dict[str, Any]]:
    return copy.deepcopy(store.internal_rows(EXECUTION_TABLE))


def list_cleanup_batches() -> list[dict[str, Any]]:
    return copy.deepcopy(store.internal_rows(CLEANUP_TABLE))


def _next_internal_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.internal_rows(table)), default=0) + 1


def _create_batch(window_id: str, ticket: dict[str, Any], operator: str) -> dict[str, Any]:
    """按 (window, snapshot) 幂等落库清理批次，并同步快照上的释放标记。"""
    batch_id = cleanup_batch_id(window_id, ticket["snapshot_no"])
    existing = store.find_internal(CLEANUP_TABLE, 批次编号=batch_id)
    if existing is not None:
        return existing
    batch = {
        "id": _next_internal_id(CLEANUP_TABLE),
        "批次编号": batch_id,
        "window_id": window_id,
        "ticket_id": ticket["ticket_id"],
        "快照编号": ticket["snapshot_no"],
        "表名称": ticket["table_name"],
        "预计回收量GB": ticket["estimated_reclaim_gb"],
        "预计回收量": ticket["estimated_reclaim_text"],
        "状态": "已创建",
        "执行人": operator,
        "created_at": _now(),
    }
    store.internal_rows(CLEANUP_TABLE).append(batch)
    for snapshot in store.rows(SNAPSHOT_MODULE):
        if str(snapshot.get("快照编号") or "") == ticket["snapshot_no"]:
            snapshot["冻结标记"] = "已释放"
            snapshot["释放结论"] = f"已创建清理批次 {batch_id}"
            snapshot["快照状态"] = "已清理"
            snapshot["status"] = "已清理"
            snapshot["pending"] = False
            snapshot["清理批次"] = batch_id
    return batch


def execute_window(
    window_id: str,
    ticket_ids: list[str] | None,
    operator: str,
) -> dict[str, Any]:
    """按窗口票幂等创建清理批次；遇到第一个失败窗口即停下，保留已完成部分。

    并发提交同一窗口时只会有一条执行记录落库：后来者复用这条记录继续，
    不会新建第二条。已完成的票直接跳过，失败票在下一次提交时从该票继续。
    """
    with _EXECUTION_LOCK:
        execution = store.find_internal(EXECUTION_TABLE, window_id=window_id)
        if execution is None:
            if window_id != current_window():
                return {
                    "ok": False,
                    "message": f"盘点窗口 {window_id} 不是当前窗口 {current_window()}，且没有可续跑的执行记录",
                    "execution": None,
                }
            plan = build_plan(window_id)
            execution = {
                "id": _next_internal_id(EXECUTION_TABLE),
                "window_id": window_id,
                "window_label": plan["window_label"],
                "operator": operator,
                "status": "进行中",
                "created_at": _now(),
                "updated_at": _now(),
                "failed_ticket_id": None,
                "attempts": 1,
                "plan_archive": copy.deepcopy(plan),
                "tickets": [],
            }
            store.internal_rows(EXECUTION_TABLE).append(execution)
        else:
            if execution["status"] == "已完成":
                # 已整体收口的窗口再提交只回读结果，不重复处理任何已完成部分。
                return {
                    "ok": True,
                    "message": f"窗口 {window_id} 已完成，已完成部分不重复处理",
                    "execution": copy.deepcopy(execution),
                }
            execution["attempts"] = int(execution.get("attempts") or 0) + 1
            execution["operator"] = operator
            execution["status"] = "进行中"
            execution["updated_at"] = _now()

        plan = build_plan(window_id)
        plan_by_id = {ticket["ticket_id"]: ticket for ticket in plan["tickets"]}

        # 计划外的新票补进执行记录；已存在的票保留历史状态。
        known = {item["ticket_id"] for item in execution["tickets"]}
        for ticket in plan["tickets"]:
            if ticket["ticket_id"] not in known:
                execution["tickets"].append({
                    "ticket_id": ticket["ticket_id"],
                    "snapshot_no": ticket["snapshot_no"],
                    "release_class": ticket["release_class"],
                    "estimated_reclaim_text": ticket["estimated_reclaim_text"],
                    "status": "待执行",
                    "清理批次编号": None,
                    "message": "",
                    "updated_at": _now(),
                })

        if ticket_ids is None:
            requested = [ticket["ticket_id"] for ticket in plan["tickets"]]
            strict_class = None
            auto_run = True
        else:
            requested = [str(item) for item in ticket_ids]
            strict_class = CLASS_RELEASABLE
            auto_run = False

        completed_now = 0
        for ticket_id in requested:
            entry = next(
                (item for item in execution["tickets"] if item["ticket_id"] == ticket_id),
                None,
            )
            if entry is None:
                return {
                    "ok": False,
                    "message": f"窗口票 {ticket_id} 不在窗口 {window_id} 的释放计划内，已停止后续处理",
                    "execution": copy.deepcopy(execution),
                }
            if entry["status"] in {"已完成", "已跳过"}:
                continue

            ticket = plan_by_id.get(ticket_id)
            current = evaluate_snapshot_no(entry["snapshot_no"]) if entry["snapshot_no"] else None
            batch_id = cleanup_batch_id(window_id, entry["snapshot_no"] or "")
            if ticket is None and (
                current is None
                or current["release_class"] == "已释放"
                or store.find_internal(CLEANUP_TABLE, 批次编号=batch_id) is not None
            ):
                # 快照已被释放：这张票由其他执行先收口了，幂等跳过而不是报错。
                entry.update({
                    "status": "已完成",
                    "清理批次编号": batch_id if store.find_internal(CLEANUP_TABLE, 批次编号=batch_id) else entry["清理批次编号"],
                    "message": "快照已按清理窗口释放，幂等跳过",
                    "updated_at": _now(),
                })
                completed_now += 1
                continue
            if ticket is None:
                entry.update({
                    "status": "失败",
                    "message": "窗口票已不在当前释放计划内，复核后重试",
                    "updated_at": _now(),
                })
                execution.update(status="已中止", failed_ticket_id=ticket_id, updated_at=_now())
                return {
                    "ok": False,
                    "message": f"窗口 {ticket_id} 校验失败，已停止后续处理；可从该窗口重试",
                    "execution": copy.deepcopy(execution),
                }
            if strict_class is not None and ticket["release_class"] != strict_class:
                reason = "；".join(ticket["cannot_release_reasons"]) or ticket["conclusion"]
                entry.update({
                    "status": "失败",
                    "message": f"窗口当前为「{ticket['release_class']}」，不可执行：{reason}",
                    "updated_at": _now(),
                })
                execution.update(status="已中止", failed_ticket_id=ticket_id, updated_at=_now())
                return {
                    "ok": False,
                    "message": f"窗口 {ticket_id} 为「{ticket['release_class']}」，已停止后续处理；可从该窗口重试",
                    "execution": copy.deepcopy(execution),
                }
            if not ticket["executable"]:
                reason = "；".join(ticket["cannot_release_reasons"]) or ticket["conclusion"]
                entry.update({
                    "status": "已跳过" if auto_run else "失败",
                    "release_class": ticket["release_class"],
                    "message": f"该窗口保持「{ticket['release_class']}」：{reason}",
                    "updated_at": _now(),
                })
                if auto_run:
                    # 冻结/待验证窗口在设计上就不进入本窗口释放，记为保留而不是失败。
                    continue
                execution.update(status="已中止", failed_ticket_id=ticket_id, updated_at=_now())
                return {
                    "ok": False,
                    "message": f"窗口 {ticket_id} 为「{ticket['release_class']}」，已停止后续处理；可从该窗口重试",
                    "execution": copy.deepcopy(execution),
                }
            if current is None or current["release_class"] != CLASS_RELEASABLE:
                reason = "；".join(current["cannot_release_reasons"]) if current else "快照已不存在"
                entry.update({
                    "status": "失败",
                    "message": f"复核未通过：{reason}",
                    "updated_at": _now(),
                })
                execution.update(status="已中止", failed_ticket_id=ticket_id, updated_at=_now())
                return {
                    "ok": False,
                    "message": f"窗口 {ticket_id} 复核未通过，已停止后续处理；可从该窗口重试",
                    "execution": copy.deepcopy(execution),
                }

            batch = _create_batch(window_id, ticket, operator)
            entry.update({
                "status": "已完成",
                "release_class": ticket["release_class"],
                "清理批次编号": batch["批次编号"],
                "message": f"已创建清理批次 {batch['批次编号']}",
                "updated_at": _now(),
            })
            completed_now += 1

        all_done = all(item["status"] in {"已完成", "已跳过"} for item in execution["tickets"])
        execution["status"] = "已完成" if all_done else "已中止"
        execution["failed_ticket_id"] = None
        execution["updated_at"] = _now()
        if all_done:
            skipped = sum(1 for item in execution["tickets"] if item["status"] == "已跳过")
            message = f"窗口 {window_id} 已按 {completed_now} 张窗口票完成清理批次创建"
            if skipped:
                message += f"；{skipped} 张冻结/待验证票保持原状"
        else:
            message = (
                f"窗口 {window_id} 本次处理 {completed_now} 张窗口票；"
                f"仍有窗口未收口，可调整后从该窗口重试"
            )
        return {"ok": all_done, "message": message, "execution": copy.deepcopy(execution)}


def manual_release(snapshot_no: str, operator: str) -> tuple[bool, str]:
    """手工清理单条快照：仍然走同一判定口径与幂等批次，避免绕过释放计划。

    返回 (是否成功, 可读说明)；快照已被释放时幂等返回成功，不重复建批次。
    """
    with _EXECUTION_LOCK:
        evaluated = evaluate_snapshot_no(snapshot_no)
        if evaluated is None:
            return False, f"快照 {snapshot_no} 不存在或已归档"
        if evaluated["release_class"] == "已释放":
            return True, f"快照 {snapshot_no} 已按清理窗口释放，无需重复处理"
        if evaluated["release_class"] != CLASS_RELEASABLE:
            reasons = "；".join(evaluated["cannot_release_reasons"]) or evaluated["conclusion"]
            return False, f"快照 {snapshot_no} 当前为「{evaluated['release_class']}」，不能清理：{reasons}"
        window = current_window()
        ticket = _ticket(window, evaluated)
        batch = _create_batch(window, ticket, operator)
        return True, f"已创建清理批次 {batch['批次编号']}"


class ReleaseService:
    def plan(self) -> dict[str, Any]:
        return build_plan()

    def execute(self, window_id: str, ticket_ids: list[str] | None, operator: str) -> dict[str, Any]:
        return execute_window(window_id, ticket_ids, operator)

    def executions(self) -> list[dict[str, Any]]:
        return list_executions()

    def batches(self) -> list[dict[str, Any]]:
        return list_cleanup_batches()


release_service = ReleaseService()
