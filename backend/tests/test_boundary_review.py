"""围栏边界归并、迟到质检与告警撤回的回归测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


FENCE_VERTICES = "39.9,116.4;39.92,116.4;39.92,116.42;39.9,116.42"


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import app.services.base as base_mod
    monkeypatch.setattr(base_mod, "DATA_ROOT", tmp_path)
    for sub in [
        "geo_points", "dedup_merged", "geofence_hits", "boundary_review",
        "quality_corrections", "withdrawals",
    ]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def write_points(root: Path, batch: str, points: list[dict]) -> None:
    path = root / "geo_points" / f"{batch}.jsonl"
    path.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in points), encoding="utf-8")


def boundary_points() -> list[dict]:
    return [
        {"point_id": "OUT", "terminal": "T1", "timestamp": 1_000,
         "lat": 39.89995, "lon": 116.41, "accuracy": 3,
         "quality_status": "可用", "location_quality_basis": "RTK"},
        {"point_id": "EDGE_IN", "terminal": "T1", "timestamp": 1_010,
         "lat": 39.90005, "lon": 116.41, "accuracy": 5},
        {"point_id": "DEEP_IN", "terminal": "T1", "timestamp": 1_020,
         "lat": 39.90035, "lon": 116.41, "accuracy": 4},
    ]


def test_boundary_pair_keeps_raw_evidence_and_single_hit(data_root: Path) -> None:
    from app.services.dedup_merge import DedupMergeService
    from app.services.geofence_rule import GeofenceRuleService

    batch = "BOUNDARY-PAIR"
    write_points(data_root, batch, boundary_points()[:2])

    dedup = DedupMergeService()
    merged = dedup.dedup_batch(batch, time_tolerance=3_600, dist_tolerance=100)
    assert merged["original_count"] == 2
    assert merged["merged_count"] == 1

    fence = GeofenceRuleService()
    result = fence.check_points_against_fence(batch, vertices=FENCE_VERTICES)
    assert result["hits"] == 1
    hit = result["hit_records"][0]
    assert hit["raw_point_ids"] == ["OUT", "EDGE_IN"]
    assert hit["representative_point_id"] == "EDGE_IN"
    assert hit["provenance"]


def test_in_flight_calculation_uses_late_quality_correction(data_root: Path) -> None:
    from app.services.boundary_review import BoundaryReviewService
    from app.services.geo_quality import GeoQualityService
    from app.services.geofence_rule import GeofenceRuleService

    batch = "LATE-QC"
    write_points(data_root, batch, boundary_points())
    review = BoundaryReviewService()

    calc = review.start_calculation(
        batch, vertices=FENCE_VERTICES, time_tolerance=3_600, dist_tolerance=100)
    assert calc["initial_pending_review_count"] == 1

    qc = GeoQualityService()
    confirmed = qc.confirm_point_quality(batch, ["EDGE_IN", "DEEP_IN"], basis="质检更正：两点可用")
    assert confirmed["confirmed_count"] == 2

    result = review.submit_calculation(calc["calculation_id"], vertices=FENCE_VERTICES)
    assert result["emitted_hits"] == 1
    assert len(result["emitted_hits"]) == 1
    assert result["adopted_representative_id"] == "DEEP_IN"
    assert result["next_review_count"] == 0

    group = result["groups"][0]
    assert group["representative_point_id"] == "OUT"
    assert group["old_representative_still_valid"] is True
    assert {c["representative_point_id"] for c in group["candidate_representatives"]} == {
        "OUT", "DEEP_IN"}

    patrol = GeofenceRuleService().patrol(batch, vertices=FENCE_VERTICES)
    assert patrol["approved_representatives"][0]["adopted_representative_id"] == "DEEP_IN"
    assert patrol["provenance"][0]


def test_dispatched_alert_gets_queryable_withdrawal_but_unsent_does_not(
        data_root: Path) -> None:
    from app.services.geofence_rule import GeofenceRuleService
    from app.services.geo_quality import GeoQualityService

    batch = "ALERT-WITHDRAW"
    write_points(data_root, batch, [
        {"point_id": "OUT", "terminal": "T9", "timestamp": 1,
         "lat": 39.89995, "lon": 116.41,
         "quality_status": "可用", "location_quality_basis": "RTK"},
        {"point_id": "IN", "terminal": "T9", "timestamp": 2,
         "lat": 39.90005, "lon": 116.41},
    ])
    fence = GeofenceRuleService()
    dispatched = fence.dispatch_alert(
        batch_key=batch, fence_id=1, group_id="T9#G001", officer="值班员甲")
    unsent = fence.register_unsent_alert(
        batch_key=batch, fence_id=1, group_id="T9#G001")

    GeoQualityService().confirm_point_quality(
        batch, ["IN"], usable=False, status="不可用", basis="质检驳回")
    result = fence.recompute_boundary(
        batch, vertices=FENCE_VERTICES, time_tolerance=3_600, dist_tolerance=100)

    assert result["hits"] == 0
    assert len(result["withdrawals"]) == 1
    withdrawal = result["withdrawals"][0]
    assert withdrawal["alert_id"] == dispatched["alert_id"]
    assert withdrawal["original_alert_id"] == dispatched["alert_id"]
    assert withdrawal["issued_to"] == "值班员甲"
    assert withdrawal["queryable"] is True

    reopened = fence.reopen_alert(dispatched["alert_id"])
    assert reopened["status"] == "已撤回"
    assert reopened["withdrawal_id"] == withdrawal["withdrawal_id"]
    assert reopened["evidence_snapshot"] is not None
    assert "撤回" in reopened["review_conclusion"]

    assert fence.get_alert(unsent["alert_id"])["status"] == "已取消"
    assert result["skipped_withdrawals"][0]["withdrawal_required"] is False
    assert fence.list_withdrawals(alert_id=unsent["alert_id"]) == []
