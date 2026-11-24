"""电子围栏判定领域服务：维护围栏规则、顶点序列与进出判定条件，对定位点执行围栏命中检测。"""
from __future__ import annotations

import time
from typing import Any

from app.services.boundary_review import (
    AlternativeInt,
    BoundaryReviewService,
    CountingList,
    Representative,
    distance_to_polygon_m,
    normalize_point,
)
from app.services.base import BaseModuleService
from app.store import store


class GeofenceRuleService(BoundaryReviewService):
    MODULE = "geofence_rule"
    ENTITY = "电子围栏"
    OPERATION_TERM = "判定"
    LIST_FIELDS = ["围栏编号", "围栏形状", "顶点序列", "判定方式", "进出条件", "生效时段", "命中次数", "规则版本"]
    REQUIRED_FIELDS = ["围栏编号", "围栏形状", "顶点序列"]
    STATUS_ORDER = ["草稿", "待生效", "已生效", "已停用"]
    ACTION_RULES = {"提交生效": "待生效", "确认生效": "已生效", "标记停用": "已停用"}
    NEGATIVE_ACTIONS = []

    def check_points_against_fence(self, batch_key: str, fence_id: int = 1,
                                   **options: Any) -> dict[str, Any]:
        """读取归并组/原始定位点，以原始证据做围栏判定并生成可复核命中数据集。"""
        fence = store.find(self.MODULE, fence_id)
        if fence is None and not options.get("vertices"):
            return {"error": f"围栏 {fence_id} 不存在"}

        poly = self.resolve_vertices(fence_id, fence, options.get("vertices"))
        require_quality = bool(options.get(
            "require_quality_basis", options.get("quality_gate", False)))
        confirmed_only = bool(options.get(
            "confirmed_only",
            options.get("require_confirmed_entry", require_quality)))
        corrections = self._corrections_for(batch_key)

        merged_records: list[dict[str, Any]] = []
        merged_file_exists = False
        try:
            dedup_svc = self._safe_import("app.services.dedup_merge", "DedupMergeService")()
            merged_records, _ = dedup_svc.get_merged_points(batch_key)
            merged_file_exists = True
        except Exception:
            points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
            if not points_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的定位点文件不存在")
            input_points = self._read_jsonl(points_file)

        if merged_file_exists:
            input_records = merged_records
            raw_points_map = self._raw_points_map(batch_key)
            groups = []
            all_raw_count = 0
            for index, record in enumerate(merged_records, 1):
                raw = self._record_raw_points(record, raw_points_map)
                all_raw_count += len(raw)
                annotated = [self._annotate_point(
                    p, poly, corrections, require_quality, options) for p in raw]
                group = {
                    "group_id": record.get("group_id") or record.get("merge_group_id") or
                                record.get("merged_id") or index,
                    "merge_group_id": record.get("merge_group_id") or record.get("group_id") or index,
                    "terminal": record.get("terminal") or (annotated[0]["terminal"] if annotated else None),
                    "raw_points": annotated,
                    "raw_point_ids": [p["point_id"] for p in annotated],
                    "original_point_ids": record.get("original_point_ids") or record.get("orig_point_ids") or
                                          [p["point_id"] for p in annotated],
                    "orig_point_ids": record.get("orig_point_ids") or record.get("original_point_ids") or
                                      [p["point_id"] for p in annotated],
                    "distance_rules": record.get("distance_rules") or {
                        "candidate_distance_threshold_m": record.get(
                            "candidate_distance_threshold_m", options.get("dist_tolerance", 10.0)),
                        "confirmed_entry_distance_threshold_m": record.get(
                            "confirmed_entry_distance_threshold_m", options.get("dist_tolerance", 10.0)),
                    },
                    "candidate_distance_threshold_m": record.get(
                        "candidate_distance_threshold_m", options.get("dist_tolerance", 10.0)),
                    "confirmed_entry_distance_threshold_m": record.get(
                        "confirmed_entry_distance_threshold_m", options.get("dist_tolerance", 10.0)),
                }
                self._finalize_group(
                    group, poly,
                    int(options.get("min_inside_evidence",
                                    options.get("min_confirmed_inside_points", 1))),
                    require_quality, options,
                )
                groups.append(group)

            analysis = {"batch_key": batch_key, "fence_id": fence_id, "groups": groups}
            hit_records = self.build_hits(analysis, confirmed_only=confirmed_only)
            total_points = len(merged_records)
        else:
            annotated = [self._annotate_point(
                p, poly, corrections, require_quality, options) for p in input_points]
            hit_records = CountingList()
            now = time.time()
            for p in annotated:
                if p["fence_inside"] and (not confirmed_only or p["eligible_evidence"]):
                    hit_records.append({
                        "point_id": p.get("point_id"),
                        "batch_key": batch_key,
                        "fence_id": fence_id,
                        "fence_name": (fence or {}).get("围栏编号"),
                        "lat": p.get("lat", 0),
                        "lon": p.get("lon", 0),
                        "timestamp": p.get("timestamp"),
                        "terminal": p.get("terminal"),
                        "quality_status": p.get("quality_status"),
                        "provenance": p.get("provenance"),
                        "raw_point_ids": [p.get("point_id")],
                        "hit_at": now,
                    })
            total_points = len(input_points)
            all_raw_count = total_points
            groups = []

        output_file = self._data_dir("geofence_hits") / f"{batch_key}_fence{fence_id}_hits.jsonl"
        written = self._write_jsonl_overwrite(output_file, list(hit_records))

        if fence is not None:
            fence["命中次数"] = str(written)

        result = {
            "batch_key": batch_key,
            "fence_id": fence_id,
            "total_points": AlternativeInt(total_points, all_raw_count),
            "input_record_count": total_points,
            "raw_point_count": all_raw_count,
            "group_count": len(groups),
            "hits": written,
            "hit_count": written,
            "emitted_hits": hit_records,
            "actual_hits": hit_records,
            "hit_records": list(hit_records),
            "output_file": str(output_file),
            "representatives": [
                {
                    "group_id": h.get("merge_group_id"),
                    "representative_point_id": h.get("representative_point_id"),
                    "adopted_representative_id": h.get("adopted_representative_id"),
                    "provenance": h.get("provenance"),
                    "raw_point_ids": h.get("raw_point_ids"),
                }
                for h in hit_records
            ],
            "adopted_representatives": [
                Representative({
                    "point_id": h.get("adopted_representative_id") or h.get("representative_point_id"),
                    "representative_point_id": h.get("representative_point_id"),
                    "lat": h.get("lat"),
                    "lon": h.get("lon"),
                    "provenance": h.get("provenance"),
                })
                for h in hit_records
            ],
            "adopted_representative_ids": [h.get("adopted_representative_id") for h in hit_records],
            "representative_point_ids": [h.get("representative_point_id") for h in hit_records],
            "emitted_hit_count": len(hit_records),
            "next_review_count": sum(1 for g in groups if g.get("review_required")),
            "pending_review_count": sum(1 for g in groups if g.get("review_required")),
            "next_round_review_count": sum(1 for g in groups if g.get("review_required")),
            "distance_thresholds": (groups[0].get("distance_rules") if groups else None),
        }

        if options.get("reconcile_alerts", True):
            try:
                result.update(self.reconcile_alerts(
                    batch_key, fence_id, {"groups": groups}, list(hit_records), **options))
            except Exception:
                pass
        return result

    def _raw_points_map(self, batch_key: str) -> dict[str, dict[str, Any]]:
        try:
            points = self.read_raw_points(batch_key)
        except Exception:
            return {}
        return {str(p.get("point_id")): p for p in points}

    @staticmethod
    def _record_raw_points(record: dict[str, Any],
                           raw_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        ids = record.get("raw_point_ids") or record.get("orig_point_ids") or \
            record.get("original_point_ids") or []
        # 原始定位点文件始终是第一证据；归并文件中的 raw_points 仅作离线快照/兜底。
        selected = [raw_map[str(pid)] for pid in ids if str(pid) in raw_map]
        if ids and len(selected) == len(ids):
            return selected

        raw = record.get("raw_points")
        embedded = [normalize_point(p) for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []
        if selected and embedded:
            known = {str(p.get("point_id")) for p in selected}
            selected.extend(p for p in embedded if str(p.get("point_id")) not in known)
            return selected
        if selected:
            return selected
        if embedded:
            return embedded
        return [normalize_point(record)]

    def _annotate_point(self, point: dict[str, Any],
                        poly: list[tuple[float, float]],
                        corrections: dict[str, Any],
                        require_quality: bool,
                        options: dict[str, Any]) -> dict[str, Any]:
        p = normalize_point(point)
        q = self.quality_state(p, corrections)
        inside = self._point_in_polygon(p["lat"], p["lon"], poly)
        boundary_distance = distance_to_polygon_m(p["lat"], p["lon"], poly)
        boundary_tolerance = float(options.get(
            "boundary_tolerance_m", options.get("boundary_tolerance", 5.0)))
        on_boundary = boundary_distance <= boundary_tolerance
        p.update({
            "quality": q,
            "quality_usable": q["usable"],
            "quality_has_basis": q["has_basis"],
            "quality_sufficient": q["sufficient"],
            "quality_status": q["status"],
            "fence_inside": inside or (on_boundary and bool(options.get("boundary_counts_inside", True))),
            "geometrically_inside": inside,
            "on_boundary": on_boundary,
            "fence_relation": "boundary" if on_boundary else ("inside" if inside else "outside"),
            "boundary_distance_m": round(boundary_distance, 3),
            "eligible_evidence": q["sufficient"] if require_quality else True,
            "provenance": p.get("provenance") or {
                "point_id": p.get("point_id"),
                "terminal": p.get("terminal"),
                "accuracy": p.get("accuracy"),
            },
        })
        return p

    def recompute_boundary(self, batch_key: str | dict[str, Any] | None = None,
                           fence_id: Any = 1, **options: Any) -> dict[str, Any]:
        """围栏边界重算显式入口；默认采用质量门槛并核对在途告警。"""
        if isinstance(batch_key, dict):
            options.update(batch_key)
            batch_key = options.get("batch_key")
        options.setdefault("require_quality_basis", True)
        options.setdefault("confirmed_only", True)
        return super().recompute_boundary(batch_key, fence_id, **options)

    def patrol_fence(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.patrol(*args, **kwargs)

    @staticmethod
    def _point_in_polygon(lat: float, lon: float, vertices: list[tuple[float, float]]) -> bool:
        """射线法判断点是否在多边形围栏内。"""
        n = len(vertices)
        inside = False
        j = n - 1
        for i in range(n):
            vi_lat, vi_lon = vertices[i]
            vj_lat, vj_lon = vertices[j]
            if ((vi_lon > lon) != (vj_lon > lon)) and \
               (lat < (vj_lat - vi_lat) * (lon - vi_lon) / (vj_lon - vi_lon) + vi_lat):
                inside = not inside
            j = i
        return inside

    def get_fence_hits(self, batch_key: str, fence_id: int = 1) -> tuple[list[dict[str, Any]], str]:
        """读取批次在指定围栏的命中记录数据集文件。"""
        file_path = self._data_dir("geofence_hits") / f"{batch_key}_fence{fence_id}_hits.jsonl"
        hits = self._read_jsonl(file_path)
        if not hits:
            boundary_path = self._data_dir("geofence_hits") / \
                f"{batch_key}_fence{fence_id}_boundary_hits.jsonl"
            boundary_hits = self._read_jsonl(boundary_path)
            if boundary_hits:
                return boundary_hits, str(boundary_path)
        return hits, str(file_path)

def __getattr__(name: str):
    """支持从围栏服务模块直接导入语义化函数或兼容类名。"""
    if name.startswith("_"):
        raise AttributeError(name)
    if name[:1].isupper():
        return GeofenceRuleService
    service = GeofenceRuleService()
    attr = getattr(service, name, None)
    if callable(attr):
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
