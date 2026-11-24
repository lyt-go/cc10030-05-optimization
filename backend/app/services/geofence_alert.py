"""围栏告警资源服务：通用资源路由使用，证据/撤回逻辑复用 BoundaryReviewService。"""
from __future__ import annotations

from typing import Any

from app.services.boundary_review import DISPATCHED_STATUS, UNSENT_STATUS, BoundaryReviewService


class GeofenceAlertService(BoundaryReviewService):
    MODULE = "geofence_alert"
    ENTITY = "围栏告警"
    LIST_FIELDS = ["告警编号", "批次编号", "围栏编号", "终端编号", "值班员", "撤回编号"]
    REQUIRED_FIELDS = ["批次编号"]
    STATUS_ORDER = ["待派出", "已派出", "在途", "已保留", "已撤回", "已取消"]
    ACTION_RULES = {
        "派出告警": "已派出",
        "标记在途": "在途",
        "保留告警": "已保留",
        "撤回告警": "已撤回",
        "取消告警": "已取消",
    }

    def list_entries(self, *, filters=None, keyword=None, status=None, page=1, size=20):
        # 通用表格仍从 store 读取；同时确保状态台账中的告警有镜像行。
        rows, total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size)
        return rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = super().get_entry(entry_id)
        if row is None:
            return self.get_alert(entry_id)
        alert_id = row.get("alert_id") or row.get("告警编号") or entry_id
        rich = self.get_alert(alert_id)
        if isinstance(rich, dict):
            merged = {**row, **rich}
            # 通用资源路由的数字 id 必须保持为 store 行 id。
            merged["id"] = row.get("id", rich.get("id", entry_id))
            return merged
        return row

    def create_entry(self, values: dict[str, Any]):
        entry, missing = super().create_entry(values)
        if entry is not None:
            self.create_alert({
                "id": entry.get("id"),
                "alert_id": values.get("alert_id") or values.get("告警编号") or entry.get("id"),
                "batch_key": values.get("batch_key") or values.get("批次编号"),
                "fence_id": values.get("fence_id") or values.get("围栏编号") or 1,
                "terminal": values.get("terminal") or values.get("终端编号"),
                "officer": values.get("officer") or values.get("值班员"),
                "group_id": values.get("group_id") or values.get("归并组"),
                "status": entry.get("status"),
                "dispatched": entry.get("status") not in UNSENT_STATUS,
                "message": values.get("message") or values.get("告警内容"),
            })
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        row = self.get_entry(entry_id)
        alert_id = row.get("alert_id") if row else None
        if alert_id is None and row is not None:
            alert_id = row.get("告警编号") or entry_id
        if action == "撤回告警" and alert_id is not None:
            result = self.withdraw_alert(alert_id)
            message = result.get("message") or (
                "尚未派出的告警无需撤回" if result.get("skipped") else "告警已撤回")
            return self.get_entry(entry_id), message
        if action == "保留告警" and alert_id is not None:
            alert = self.get_alert(alert_id)
            if alert:
                alert["review_status"] = "已保留"
                alert["retained"] = True
                alert["retention_basis"] = "人工复核后仍保留原告警依据"
                state = self._load_state()
                state["alerts"][str(alert_id)] = alert
                self._save_state(state)
                self._mirror_alert_to_store(alert)
            entry, message = super().run_action(entry_id, action)
            return entry, "告警已保留"

        entry, message = super().run_action(entry_id, action)
        if entry is not None and alert_id is not None:
            state = self._load_state()
            alert = state["alerts"].get(str(alert_id))
            if alert is None:
                alert = self.get_alert(alert_id)
            if isinstance(alert, dict):
                alert["status"] = entry.get("status", alert.get("status"))
                alert["dispatched"] = alert["status"] in DISPATCHED_STATUS
                if alert["dispatched"] and not alert.get("dispatched_at"):
                    import time
                    alert["dispatched_at"] = time.time()
                state["alerts"][str(alert_id)] = alert
                self._save_state(state)
                self._mirror_alert_to_store(alert)
        return entry, message
