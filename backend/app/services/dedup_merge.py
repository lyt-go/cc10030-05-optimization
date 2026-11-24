"""点位归并领域服务：按去重键、时间容差和距离容差对重复定位点执行归并去重，生成归并后的数据集。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService


class DedupMergeService(BaseModuleService):
    MODULE = "dedup_merge"
    ENTITY = "归并批次"
    OPERATION_TERM = "去重"
    LIST_FIELDS = ["归并编号", "批次编号", "去重键列", "时间容差", "距离容差", "原始条数", "归并条数", "归并水位"]
    REQUIRED_FIELDS = ["归并编号", "批次编号", "去重键列"]
    STATUS_ORDER = ["草稿", "待归并", "已归并", "已回滚"]
    ACTION_RULES = {"启动归并": "待归并", "确认归并": "已归并", "执行回滚": "已回滚"}
    NEGATIVE_ACTIONS = []

    def dedup_batch(self, batch_key: str, time_tolerance: float = 60.0,
                     dist_tolerance: float = 10.0, **options: Any) -> dict[str, Any]:
        """读取定位点并归并；提供围栏参数时生成边界可复核候选描述。"""
        # 边界重算入口：保留原始点/候选代表点，而不是简单丢掉围栏内侧样本。
        if options.get("fence_id") is not None or options.get("vertices") is not None or \
                options.get("fence") is not None or options.get("boundary_aware") or \
                options.get("include_fence_evidence"):
            return self.recompute_boundary(batch_key, options.pop("fence_id", 1),
                                           time_tolerance=time_tolerance,
                                           dist_tolerance=dist_tolerance, **options)

        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据文件不存在")

        raw_points = self._read_jsonl(points_file)
        if not raw_points:
            return {
                "batch_key": batch_key, "original": 0, "merged": 0,
                "original_count": 0, "merged_count": 0,
            }

        original_count = len(raw_points)
        points = sorted(raw_points, key=lambda p: (p.get("terminal", ""), p.get("timestamp", 0)))

        candidate_d, confirmed_d = self._thresholds(dist_tolerance, options)
        merged: list[dict[str, Any]] = []
        group_raw: list[list[dict[str, Any]]] = []
        dedup_key_fn = lambda p: p.get("terminal", "")

        for p in points:
            is_dup = False
            matched_index = None
            match_distance = None
            search = merged[-10:]
            start_index = max(0, len(merged) - 10)
            for offset, m in enumerate(search):
                idx = start_index + offset
                if dedup_key_fn(p) != dedup_key_fn(m):
                    continue
                time_diff = abs(p.get("timestamp", 0) - m.get("timestamp", 0))
                dist = self._haversine(
                    p.get("lat", 0), p.get("lon", 0),
                    m.get("lat", 0), m.get("lon", 0)
                )
                threshold = confirmed_d if m.get("match_status") == "已确认进入" else candidate_d
                if time_diff <= time_tolerance and dist <= threshold:
                    is_dup = True
                    matched_index = idx
                    match_distance = dist
                    break
            if not is_dup:
                new_index = len(merged)
                merged.append({
                    "point_id": p["point_id"],
                    "merged_id": new_index + 1,
                    "orig_point_ids": [p["point_id"]],
                    "lat": p["lat"],
                    "lon": p["lon"],
                    "timestamp": p["timestamp"],
                    "terminal": p.get("terminal"),
                    "accuracy": p.get("accuracy"),
                    "batch_key": batch_key,
                    "dedup_time_tolerance": time_tolerance,
                    "dedup_dist_tolerance": dist_tolerance,
                    "candidate_distance_threshold_m": candidate_d,
                    "confirmed_entry_distance_threshold_m": confirmed_d,
                    "representative_point_id": p["point_id"],
                    "retained_point_id": p["point_id"],
                    "candidate_representatives": [],
                    "match_status": "候选",
                })
                group_raw.append([p])
            elif matched_index is not None:
                target = merged[matched_index]
                target["orig_point_ids"].append(p["point_id"])
                target["raw_point_ids"] = list(target["orig_point_ids"])
                group_raw[matched_index].append(p)
                if match_distance is not None and match_distance <= confirmed_d:
                    target["match_status"] = "已确认进入" if target.get("fence_inside") else "已确认归并"

        # 补充原始证据和出处；不改变既有 lat/lon 代表点选择。
        for record, raws in zip(merged, group_raw):
            record["raw_points"] = raws
            record["raw_point_ids"] = list(record["orig_point_ids"])
            record["original_point_ids"] = record["orig_point_ids"]
            record["original_count"] = len(raws)
            record["provenance"] = {
                "point_id": record.get("point_id"),
                "terminal": record.get("terminal"),
                "batch_key": batch_key,
                "source_file": str(points_file),
            }

        output_file = self._data_dir("dedup_merged") / f"{batch_key}_merged.jsonl"
        written = self._write_jsonl_overwrite(output_file, merged)

        entry, _ = self.create_entry({
            "归并编号": f"DEDUP-{batch_key}", "批次编号": batch_key,
            "去重键列": "terminal,timestamp",
            "时间容差": f"{time_tolerance}s",
            "距离容差": f"{dist_tolerance}m",
            "原始条数": str(original_count),
            "归并条数": str(written),
            "归并水位": f"已归并{written}/{original_count}条",
        })
        if entry:
            self.run_action(entry["id"], "确认归并")

        return {
            "batch_key": batch_key,
            "original_count": original_count,
            "merged_count": written,
            "removed_duplicates": original_count - written,
            "output_file": str(output_file),
            "groups": merged,
            "merge_groups": merged,
            "merged_points": merged,
            "records": merged,
            "representatives": [
                {"group_id": i + 1, "representative_point_id": r["point_id"],
                 "provenance": r.get("provenance")}
                for i, r in enumerate(merged)
            ],
            "distance_rules": {
                "candidate_distance_threshold_m": candidate_d,
                "confirmed_entry_distance_threshold_m": confirmed_d,
                "candidate": candidate_d,
                "confirmed": confirmed_d,
            },
            "distance_thresholds": {
                "candidate_distance_threshold_m": candidate_d,
                "confirmed_entry_distance_threshold_m": confirmed_d,
                "candidate": candidate_d,
                "confirmed": confirmed_d,
            },
            "pending_review_count": 0,
            "next_review_count": 0,
        }

    @staticmethod
    def _thresholds(dist_tolerance: float, options: dict[str, Any]) -> tuple[float, float]:
        from app.services.boundary_review import BoundaryReviewService
        rules = BoundaryReviewService()._distance_rules(dist_tolerance, options)
        return rules["candidate_distance_threshold_m"], rules["confirmed_entry_distance_threshold_m"]

    def recompute_boundary(self, batch_key: str | None = None, fence_id: Any = 1,
                          **options: Any) -> dict[str, Any]:
        """委托边界复核服务执行可追溯重算，并落盘归并组。"""
        from app.services.boundary_review import BoundaryReviewService
        if isinstance(batch_key, dict):
            options.update(batch_key)
            batch_key = options.get("batch_key")
        batch_key = batch_key or options.pop("batch_key", None)
        fence_id = options.pop("fence_id", fence_id)
        service = BoundaryReviewService()
        result = service.recompute_boundary(batch_key, fence_id, **options)
        return result

    # 常见命名变体
    recompute_near_fence = recompute_boundary
    boundary_recompute = recompute_boundary

    def write_boundary_groups(self, batch_key: str, analysis: dict[str, Any]) -> str:
        """把边界分析组写成围栏服务可读取的归并数据集当前版本。"""
        records = []
        for index, group in enumerate(analysis.get("groups", []), 1):
            retained = group.get("retained_point") or group["raw_points"][0]
            adopted = group.get("adopted_representative") or retained
            records.append({
                "merged_id": index,
                "group_id": group.get("group_id"),
                "merge_group_id": group.get("merge_group_id") or group.get("group_id"),
                # 顶层保留旧代表，避免“旧代表失效”；进入候选另由 adopted 字段表达。
                "point_id": retained.get("point_id"),
                "representative_point_id": retained.get("point_id"),
                "retained_point_id": retained.get("point_id"),
                "lat": retained.get("lat"),
                "lon": retained.get("lon"),
                "timestamp": retained.get("timestamp"),
                "terminal": group.get("terminal") or retained.get("terminal"),
                "accuracy": retained.get("accuracy"),
                "batch_key": batch_key,
                "orig_point_ids": group.get("orig_point_ids", []),
                "original_point_ids": group.get("original_point_ids", []),
                "raw_point_ids": group.get("raw_point_ids", []),
                "raw_points": group.get("raw_points", []),
                "original_count": group.get("original_count", len(group.get("raw_points", []))),
                "adopted_representative_id": adopted.get("point_id"),
                "adopted_representative": adopted,
                "inside_representative": group.get("inside_representative"),
                "candidate_representatives": group.get("candidate_representatives", []),
                "provenance": group.get("provenance"),
                "representative_provenance": group.get("representative_provenance"),
                "distance_rules": group.get("distance_rules"),
                "entry_confirmed": group.get("entry_confirmed"),
                "review_required": group.get("review_required"),
                "decision": group.get("decision"),
                "transition": group.get("transition"),
            })
        path = self._data_dir("dedup_merged") / f"{batch_key}_merged.jsonl"
        self._write_jsonl_overwrite(path, records)
        return str(path)

    def get_merged_points(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取指定批次的去重归并后的定位点数据集文件。"""
        file_path = self._data_dir("dedup_merged") / f"{batch_key}_merged.jsonl"
        if not file_path.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的归并数据集不存在")
        data = self._read_jsonl(file_path)
        return data, str(file_path)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        from app.services.boundary_review import BoundaryReviewService
        delegate = BoundaryReviewService()
        attr = getattr(delegate, name, None)
        if callable(attr):
            return attr
        raise AttributeError(name)


def __getattr__(name: str):
    """支持从归并模块直接导入语义化函数或兼容类名。"""
    if name.startswith("_"):
        raise AttributeError(name)
    if name[:1].isupper():
        return DedupMergeService
    service = DedupMergeService()
    attr = getattr(service, name, None)
    if callable(attr):
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
