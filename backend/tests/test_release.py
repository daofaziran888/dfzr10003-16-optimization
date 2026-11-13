"""轻量回归检查：不起服务，直接调用业务层与 ASGI 应用验证释放计划口径。

运行：cd backend && .venv/bin/python -I tests/test_release.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.services.release import (  # noqa: E402
    CLASS_FROZEN,
    CLASS_PENDING,
    CLASS_RELEASABLE,
    build_plan,
    current_window,
    evaluate_snapshot,
    execute_window,
    release_hold_for_snapshot,
)
from app.store import store  # noqa: E402


def _rows(module: str) -> dict[str, dict]:
    return {str(row.get("快照编号") or row.get("批次编号")): row for row in store.rows(module)}


def check_plan_baseline() -> None:
    plan = build_plan()
    assert plan["read_only"] is True
    assert plan["window_id"] == current_window()
    ticket_ids = {ticket["snapshot_no"]: ticket for ticket in plan["tickets"]}
    assert set(ticket_ids) == {"SNAP-0001", "SNAP-0002", "SNAP-0004", "SNAP-0006"}, ticket_ids
    assert ticket_ids["SNAP-0001"]["release_class"] == CLASS_RELEASABLE
    assert ticket_ids["SNAP-0002"]["release_class"] == CLASS_FROZEN
    assert ticket_ids["SNAP-0006"]["release_class"] == CLASS_FROZEN
    assert ticket_ids["SNAP-0004"]["release_class"] == CLASS_PENDING
    # 预计回收量只统计真正可释放的窗口，冻结/待验证不报可回收。
    assert ticket_ids["SNAP-0001"]["estimated_reclaim_gb"] == 2.4
    assert ticket_ids["SNAP-0002"]["estimated_reclaim_gb"] == 0.0
    assert ticket_ids["SNAP-0002"]["occupancy_batches"][0]["批次编号"] == "BACKFILL-0002"
    # 恢复校验缺失但存在活跃补数引用时以占用一侧为准。
    assert ticket_ids["SNAP-0002"]["recovery"]["missing"] is True
    assert "占用" in ticket_ids["SNAP-0002"]["conclusion"]
    # 引用状态与释放标记冲突：以活跃占用一侧为准。
    assert ticket_ids["SNAP-0006"]["occupancy_batches"][0]["引用状态"] == "已释放"
    assert ticket_ids["SNAP-0006"]["occupancy_batches"][0]["释放标记"] == "占用中"
    assert "活跃" in ticket_ids["SNAP-0006"]["cannot_release_reasons"][0]
    assert plan["summary"]["releasable_size_gb"] == 2.4
    assert plan["summary"]["frozen_size_gb"] == 9.6
    assert plan["summary"]["pending_verification_size_gb"] == 0.8
    # 未到期快照不进入窗口票。
    assert {item["snapshot_no"] for item in plan["not_due"]} == {"SNAP-0003", "SNAP-0005", "SNAP-0007"}
    # 计划本身不改动业务记录。
    assert _rows("snapshot")["SNAP-0001"]["冻结标记"] is None


def check_shared_hold() -> None:
    hold_snap2 = release_hold_for_snapshot("SNAP-0002")
    assert hold_snap2["冻结标记"] == "冻结"
    hold_snap1 = release_hold_for_snapshot("SNAP-0001")
    assert hold_snap1["冻结标记"] == "可释放"
    # 同一判定口径也用于单条评估。
    assert evaluate_snapshot(_rows("snapshot")["SNAP-0002"])["release_class"] == CLASS_FROZEN
    assert release_hold_for_snapshot(None)["冻结标记"] == "未绑定快照"
    assert release_hold_for_snapshot("SNAP-9999")["冻结标记"] == "未知快照"


def check_execute_and_idempotency() -> None:
    window = build_plan()["window_id"]
    first = execute_window(window, None, "数据版本管理员")
    assert first["ok"] is True, first["message"]
    statuses = {item["ticket_id"]: item["status"] for item in first["execution"]["tickets"]}
    assert statuses[f"WIN-{window}-SNAP-0001"] == "已完成"
    assert statuses[f"WIN-{window}-SNAP-0002"] == "已跳过"
    batches = [row["批次编号"] for row in store.internal_rows("cleanup_batch")]
    assert batches == [f"CLEAN-{window}-SNAP-0001"], batches

    second = execute_window(window, None, "数据版本管理员")
    assert "已完成" in second["message"]
    assert second["execution"]["attempts"] == 2
    assert [row["批次编号"] for row in store.internal_rows("cleanup_batch")] == batches

    # 已释放后的快照页与历史重跑页读到同一冻结标记。
    assert release_hold_for_snapshot("SNAP-0001")["冻结标记"] == "已释放"
    release_plan = build_plan()
    assert release_plan["summary"]["released_count"] == 1
    assert release_plan["summary"]["releasable_count"] == 0
    assert release_plan["execution"]["status"] == "已完成"


def check_failure_stops_and_retries() -> None:
    # 直接走前端会发起的显式票提交：冻结票必须让后续票停下。
    window = current_window()
    result = execute_window(window, [f"WIN-{window}-SNAP-0002"], "数据版本管理员")
    assert result["ok"] is False
    assert result["execution"]["failed_ticket_id"] == f"WIN-{window}-SNAP-0002"
    assert result["execution"]["tickets"][1]["status"] == "失败"


def check_concurrent_single_execution() -> None:
    """同一窗口并发提交只能有一条执行记录，清理批次也不重复。"""
    window = current_window()
    results: list[dict] = []

    def worker() -> None:
        results.append(execute_window(window, None, "数据版本管理员"))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(item["ok"] for item in results)
    executions = store.internal_rows("release_execution")
    assert len(executions) == 1, executions
    assert len(store.internal_rows("cleanup_batch")) == 1


async def _call(method: str, path: str, headers: dict[str, str], body: dict | None = None):
    raw_headers = [(b"content-type", b"application/json")]
    raw_headers += [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    raw_body = json.dumps(body).encode() if body is not None else b""
    raw_headers.append((b"content-length", str(len(raw_body)).encode()))
    messages = [{"type": "http.request", "body": raw_body, "more_body": False}]
    sent: list[dict] = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": raw_headers,
        "http_version": "1.1",
        "scheme": "http",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 12345),
    }
    await app(scope, receive, send)
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    payload = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, json.loads(payload)


def check_permissions() -> None:
    admin = {"X-Operator-Role": "admin"}
    viewer = {"X-Operator-Role": "viewer"}

    status, plan = asyncio.run(_call("GET", "/api/snapshot/release-plan", viewer))
    assert status == 200, status
    assert plan["actor"]["can_cleanup"] is False
    assert plan["summary"]["releasable_count"] >= 0

    status, body = asyncio.run(_call("POST", "/api/snapshot/release-plan/execute", viewer,
                                     {"window_id": current_window()}))
    assert status == 403, body

    status, body = asyncio.run(_call("POST", "/api/snapshot/release-plan/execute", {}, {"window_id": current_window()}))
    assert status == 403, body

    status, body = asyncio.run(_call("POST", "/api/backfill/2/actions", viewer, {"action": "启动重跑"}))
    assert status == 403, body

    status, rows = asyncio.run(_call("GET", "/api/backfill", viewer))
    assert status == 200
    batch = next(item for item in rows["items"] if item["批次编号"] == "BACKFILL-0002")
    assert batch["快照冻结标记"]["冻结标记"] == "冻结"
    assert rows["actor"] == "查看者"

    # 快照列表也必须内嵌同一占用结论。
    status, snapshot_rows = asyncio.run(_call("GET", "/api/snapshot", viewer))
    assert status == 200
    snap = next(item for item in snapshot_rows["items"] if item["快照编号"] == "SNAP-0002")
    assert snap["占用结论"]["release_class"] == CLASS_FROZEN
    assert snap["占用结论"]["hold"]["冻结标记"] == "冻结"


def check_backfill_rollback_blocked_after_release() -> None:
    # 管理员把 BACKFILL-0002 指向的快照释放后，重跑侧必须读到已释放并拒绝回滚。
    store.rows("backfill")[1]["引用快照编号"] = "SNAP-0001"
    status, body = asyncio.run(_call("POST", "/api/backfill/2/actions",
                                     {"X-Operator-Role": "admin"}, {"action": "启动重跑"}))
    # 用服务层断言更直观；HTTP 层只确认拒绝原因可读。
    assert "不能再执行回滚" in body["message"], body
    status, row = asyncio.run(_call("GET", "/api/backfill/2", {}))
    assert row["快照冻结标记"]["冻结标记"] == "已释放"
    store.rows("backfill")[1]["引用快照编号"] = "SNAP-0002"


def main() -> None:
    check_plan_baseline()
    check_shared_hold()
    check_permissions()
    check_failure_stops_and_retries()
    check_execute_and_idempotency()
    check_concurrent_single_execution()
    check_backfill_rollback_blocked_after_release()
    print("release plan checks passed")


if __name__ == "__main__":
    main()
