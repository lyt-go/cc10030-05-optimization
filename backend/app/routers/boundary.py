"""边界重算、巡检和告警撤回的显式 HTTP 契约。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.services.boundary_review import BoundaryReviewService
from app.services.geofence_rule import GeofenceRuleService

router = APIRouter(prefix="/api", tags=["围栏边界复核"])


def service() -> BoundaryReviewService:
    return GeofenceRuleService()


@router.post("/boundary-review/recompute")
def recompute(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    batch_key = payload.get("batch_key") or payload.get("batchKey") or payload.get("批次编号")
    fence_id = payload.get("fence_id", payload.get("fenceId", 1))
    return service().recompute_boundary(batch_key, fence_id, **{k: v for k, v in payload.items()
                                        if k not in {"batch_key", "batchKey", "fence_id", "fenceId"}})


@router.post("/{module_key}/boundary-recompute")
def module_boundary_recompute(module_key: str,
                              payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """兼容 /api/dedup_merge/boundary-recompute 或 /api/geofence_rule/... 调用。"""
    batch_key = payload.get("batch_key") or payload.get("batchKey") or payload.get("批次编号")
    fence_id = payload.get("fence_id", payload.get("fenceId", 1))
    if module_key not in {"dedup_merge", "geofence_rule", "boundary_review"}:
        raise HTTPException(status_code=404, detail=f"模块 {module_key} 不支持边界重算")
    return service().recompute_boundary(batch_key, fence_id, **{
        k: v for k, v in payload.items()
        if k not in {"batch_key", "batchKey", "fence_id", "fenceId"}
    })


@router.post("/{module_key}/recompute-boundary")
def module_recompute_boundary_alias(module_key: str,
                                    payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return module_boundary_recompute(module_key, payload)


@router.post("/{module_key}/recompute")
def module_recompute_alias(module_key: str,
                           payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    if module_key not in {"dedup_merge", "geofence_rule", "boundary_review"}:
        raise HTTPException(status_code=404, detail=f"未知重算模块：{module_key}")
    return module_boundary_recompute(module_key, payload)


@router.post("/boundary-review/calculations")
def start_calculation(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return service().start_calculation(**payload)


@router.post("/boundary-review/calculations/{calculation_id}/submit")
def submit_calculation(calculation_id: str,
                       payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    try:
        return service().submit_calculation(calculation_id, **payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/boundary-review/calculations/{calculation_id}")
def get_calculation(calculation_id: str) -> dict[str, Any]:
    state = service()._load_state()
    calc = state["calculations"].get(str(calculation_id))
    if calc is None:
        raise HTTPException(status_code=404, detail="在途计算不存在")
    return calc


@router.get("/boundary-review/patrol")
def patrol(batch_key: str | None = Query(default=None),
           fence_id: Any = Query(default=1),
           recalculate: bool = Query(default=False)) -> dict[str, Any]:
    return service().patrol(batch_key, fence_id, recalculate=recalculate)


@router.post("/quality/{batch_key}/confirm-points")
def confirm_quality(batch_key: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return service().confirm_quality(batch_key, **payload)


@router.post("/geo_quality/confirm-points")
def confirm_quality_by_body(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    batch_key = payload.pop("batch_key", payload.pop("batchKey", None))
    return service().confirm_quality(batch_key, **payload)


@router.get("/fence-alerts")
def list_alerts(batch_key: str | None = Query(default=None),
                fence_id: Any = Query(default=None),
                group_id: Any = Query(default=None)) -> dict[str, Any]:
    items = service().list_alerts(batch_key, fence_id, group_id)
    return {"items": items, "total": len(items)}


@router.post("/fence-alerts")
def create_alert(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return service().create_alert(payload)


@router.get("/fence-alerts/{alert_id}")
def get_alert(alert_id: str) -> dict[str, Any]:
    alert = service().reopen_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="原告警不存在")
    return alert


@router.post("/fence-alerts/{alert_id}/withdraw")
def withdraw_alert(alert_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    try:
        return service().withdraw_alert(alert_id, **payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/fence-withdrawals/{withdrawal_id}")
def get_withdrawal(withdrawal_id: str) -> dict[str, Any]:
    result = service().get_withdrawal(withdrawal_id)
    if result is None:
        raise HTTPException(status_code=404, detail="撤回结果不存在")
    return result


# 语义别名：部分调用方可能使用 alerts / revocations 命名。
@router.get("/alerts")
def list_alerts_alias(batch_key: str | None = Query(default=None),
                      fence_id: Any = Query(default=None),
                      group_id: Any = Query(default=None)) -> dict[str, Any]:
    return list_alerts(batch_key, fence_id, group_id)


@router.get("/alerts/{alert_id}")
def get_alert_alias(alert_id: str) -> dict[str, Any]:
    return get_alert(alert_id)


@router.post("/alerts/{alert_id}/withdraw")
def withdraw_alert_alias(alert_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return withdraw_alert(alert_id, payload)


@router.get("/revocations/{withdrawal_id}")
def get_revocation_alias(withdrawal_id: str) -> dict[str, Any]:
    return get_withdrawal(withdrawal_id)
