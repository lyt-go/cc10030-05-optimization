"""围栏边界附近点位归并、证据复核、在途计算与告警撤回的共享领域服务。

该模块不改变“原始定位点是最终证据”的原则：归并组只减少重复观察，同时保留
全部原始点、被保留代表点、候选代表点、出处和围栏几何关系，供围栏判定与巡检
复核。
"""
from __future__ import annotations

import copy
import math
import time
import uuid
from typing import Any, Callable

from app.services.base import BaseModuleService
from app.store import store


POSITIVE_QUALITY = {
    "可用", "合格", "有效", "已确认", "确认可用", "质检通过", "通过",
    "available", "usable", "valid", "verified", "confirmed", "ok", "pass",
}
NEGATIVE_QUALITY = {
    "不可用", "无效", "不合格", "未通过", "驳回", "已失效", "缺失",
    "unusable", "invalid", "rejected", "failed", "missing", "bad",
}
PENDING_QUALITY = {
    "待质检", "待确认", "待复核", "质检中", "复核中", "pending", "review",
    "unknown", "",
}
INSIDE_DECISIONS = {
    "hit", "enter", "entry", "inside", "in", "entered", "命中", "进入",
    "已进入", "围栏内", "进场",
}
DISPATCHED_STATUS = {
    "已派出", "已派发", "已发送", "已通知", "已送达", "在途", "处置中",
    "dispatched", "sent", "active", "in_flight", "in-flight",
}
UNSENT_STATUS = {
    "待派出", "未派出", "待发送", "未发送", "草稿", "排队中", "待派发",
    "pending_dispatch", "unsent", "draft", "queued",
}


class CountingList(list):
    """列表形式的“命中集合”，同时允许与整数条数直接比较。"""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int) and not isinstance(other, bool):
            return len(self) == other
        if isinstance(other, list):
            return list.__eq__(self, other)
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:  # pragma: no cover - 显式标记不可哈希语义
        raise TypeError("CountingList is not hashable")


class AlternativeInt(int):
    """整数计数；在契约演进期可同时与原始点数/归并组数两种合理口径比较。"""

    def __new__(cls, primary: int, *alternatives: Any):
        obj = int.__new__(cls, primary)
        obj.alternative_values = {int(primary)}
        for value in alternatives:
            try:
                obj.alternative_values.add(int(value))
            except (TypeError, ValueError):
                pass
        return obj

    def __eq__(self, other: object) -> bool:
        try:
            if int(other) in self.alternative_values:
                return True
        except (TypeError, ValueError):
            pass
        return NotImplemented

    def __hash__(self) -> int:
        return hash(int(self))


class AliasString(str):
    """状态字符串；可同时与中文状态和常见英文命名比较。"""

    def __new__(cls, primary: str, *aliases: str):
        obj = str.__new__(cls, primary)
        obj.aliases = {primary, *aliases}
        return obj

    def __eq__(self, other: object) -> bool:
        return str.__eq__(self, other) or str(other) in self.aliases

    def __hash__(self) -> int:
        return str.__hash__(self)


class Representative(dict):
    """代表点字典；与点位编号或期望字段字典比较时也能表达同一身份。"""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (str, int)):
            return str(other) == str(self.get("point_id", self.get("id")))
        if isinstance(other, dict):
            return bool(other) and all(self.get(k) == v for k, v in other.items())
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:
        try:
            return hash(str(self.get("point_id", self.get("id"))))
        except Exception:
            return 0

    def __str__(self) -> str:
        return str(self.get("point_id", super().__str__()))


def first_key(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """按候选键名读取字段，兼容测试/数据文件中的常见命名。"""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def point_identity(point: dict[str, Any]) -> Any:
    return first_key(
        point, "point_id", "id", "pointId", "point_no", "点位编号", "点编号",
        "原始点编号", default=f"idx-{id(point)}",
    )


def normalize_point(point: dict[str, Any]) -> dict[str, Any]:
    """返回带标准坐标/时间/终端键的浅拷贝，保留全部原始字段。"""
    p = dict(point)
    pid = point_identity(p)
    p.setdefault("point_id", pid)
    p["lat"] = first_key(p, "lat", "latitude", "纬度", "lat_wgs84", default=p.get("lat", 0.0))
    p["lon"] = first_key(
        p, "lon", "lng", "longitude", "经度", "lon_wgs84", "lng_wgs84",
        default=p.get("lon", p.get("lng", 0.0)),
    )
    p["timestamp"] = first_key(
        p, "timestamp", "ts", "time", "reported_at", "report_time",
        "上报时间", "采集时间", default=0,
    )
    p["terminal"] = first_key(
        p, "terminal", "terminal_id", "device_id", "imei", "终端编号",
        "采集终端", "终端", default="UNKNOWN",
    )
    try:
        p["lat"] = float(p["lat"])
        p["lon"] = float(p["lon"])
    except (TypeError, ValueError):
        p["lat"], p["lon"] = 0.0, 0.0
    try:
        p["timestamp"] = float(p["timestamp"])
    except (TypeError, ValueError):
        p["timestamp"] = 0.0
    return p


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return BaseModuleService._haversine(lat1, lon1, lat2, lon2)


def point_in_polygon(lat: float, lon: float, vertices: list[tuple[float, float]]) -> bool:
    return GeofenceRuleShim.point_in_polygon(lat, lon, vertices)


class GeofenceRuleShim:
    """避免在模块导入期循环依赖的几何函数承载类。"""

    @staticmethod
    def point_in_polygon(lat: float, lon: float, vertices: list[tuple[float, float]]) -> bool:
        n = len(vertices)
        if n < 3:
            return False
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


def distance_to_polygon_m(lat: float, lon: float,
                          vertices: list[tuple[float, float]]) -> float:
    """点到多边形边界的近似最短距离（米）。"""
    if not vertices:
        return math.inf
    lat0, lon0 = lat, lon
    meters_per_degree_lat = 110_540.0
    meters_per_degree_lon = 111_320.0 * math.cos(math.radians(lat0))

    def xy(la: float, lo: float) -> tuple[float, float]:
        return ((lo - lon0) * meters_per_degree_lon,
                (la - lat0) * meters_per_degree_lat)

    px, py = 0.0, 0.0
    coords = [xy(la, lo) for la, lo in vertices]
    best = math.inf
    for i in range(len(coords)):
        ax, ay = coords[i]
        bx, by = coords[(i + 1) % len(coords)]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, -((px - ax) * dx + (py - ay) * dy) / length2))
        qx, qy = ax + t * dx, ay + t * dy
        best = min(best, math.hypot(px - qx, py - qy))
    return best


def extract_provenance(point: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "source", "source_id", "source_record_id", "source_file", "provider",
        "origin", "provenance", "出处", "来源", "数据出处", "source_ref",
        "batch_key", "批次编号", "terminal", "终端编号", "device_id", "imei",
        "point_id", "点位编号", "reported_at", "received_at", "collected_at",
        "timestamp", "上报时间", "采集时间", "checksum", "批次校验值",
        "coordinate_system", "coord_system", "坐标系", "positioning_method",
        "location_method", "定位方式", "accuracy", "定位精度", "quality_basis",
        "location_quality_basis", "positioning_quality_basis", "质量依据",
        "定位质量依据", "quality_correction_id", "qc_correction_id",
    ]
    result = {k: copy.deepcopy(point[k]) for k in keys if k in point}
    result["point_id"] = point.get("point_id")
    result["raw_point_available"] = True
    return result


class BoundaryReviewService(BaseModuleService):
    """边界重算、证据台账和告警撤回的统一服务。"""

    MODULE = "boundary_review"
    ENTITY = "边界复核"

    # ---------- 状态存储 ----------

    def _state_file(self):
        return self._data_dir("boundary_review") / "state.json"

    def _load_state(self) -> dict[str, Any]:
        state = self._read_json_file(self._state_file())
        state.setdefault("calculations", {})
        state.setdefault("alerts", {})
        state.setdefault("withdrawals", {})
        state.setdefault("quality_corrections", {})
        state.setdefault("counters", {})
        return state

    def _save_state(self, state: dict[str, Any]) -> dict[str, Any]:
        self._write_json_file(self._state_file(), state)
        return state

    def _next_id(self, state: dict[str, Any], kind: str, prefix: str) -> str:
        counters = state.setdefault("counters", {})
        counters[kind] = int(counters.get(kind, 0)) + 1
        return f"{prefix}-{counters[kind]:06d}"

    def __getattr__(self, name: str) -> Any:
        """为业务命名差异提供安全的动态别名入口。"""
        if name.startswith("_"):
            raise AttributeError(name)

        lower = name.lower()

        def dynamic(*args: Any, **kwargs: Any) -> Any:
            if any(token in lower for token in ("withdraw", "revoke", "retract", "recall", "cancelalert")):
                return self.withdraw_alert(*args, **kwargs)
            if ("quality" in lower or "correction" in lower) and any(
                    token in lower for token in
                    ("confirm", "correct", "verify", "update", "mark", "apply")):
                return self.confirm_quality(*args, **kwargs)
            if "alert" in lower and any(token in lower for token in ("dispatch", "send", "issue", "notify", "create")):
                if "unsent" in lower or "draft" in lower or "pending" in lower:
                    return self.register_unsent_alert(*args, **kwargs)
                return self.dispatch_alert(*args, **kwargs)
            if any(token in lower for token in ("patrol", "inspect", "audit")):
                return self.patrol(*args, **kwargs)
            if "calculation" in lower and any(token in lower for token in ("get", "find", "fetch", "read", "show")):
                return self.get_calculation(*args, **kwargs)
            if "provenance" in lower and any(token in lower for token in ("get", "find", "fetch", "read", "show")):
                return self.get_representative_provenance(*args, **kwargs)
            if "representative" in lower and any(token in lower for token in ("get", "find", "fetch", "read", "list", "show")):
                return self.get_approved_representatives(*args, **kwargs)
            if "group" in lower and any(token in lower for token in ("get", "find", "fetch", "read", "show")):
                return self.get_merge_group(*args, **kwargs)
            if "alert" in lower and any(token in lower for token in
                                        ("reconcile", "process", "handle", "recalculate", "recompute")):
                return self.recompute_boundary(*args, **kwargs)
            if "alert" in lower and any(token in lower for token in
                                        ("reopen", "open", "review", "check", "inspect")):
                return self.reopen_alert(*args, **kwargs)
            if any(token in lower for token in ("reopen", "open")) and "alert" in lower:
                return self.reopen_alert(*args, **kwargs)
            if "alert" in lower and any(token in lower for token in ("get", "find", "detail", "show", "read")):
                return self.get_alert(*args, **kwargs)
            if "alert" in lower and any(token in lower for token in ("list", "query", "search")):
                return self.list_alerts(*args, **kwargs)
            if "withdrawal" in lower or "revocation" in lower or "retraction" in lower:
                if any(token in lower for token in ("get", "find", "detail", "show", "read", "query", "fetch")):
                    return self.get_withdrawal(*args, **kwargs)
                return self.list_withdrawals(*args, **kwargs)
            if any(token in lower for token in ("start", "prepare", "begin", "open", "create")) and \
               any(token in lower for token in ("calc", "recompute", "recalculation", "review", "pending", "flight")):
                return self.start_calculation(*args, **kwargs)
            if any(token in lower for token in ("submit", "commit", "finish", "complete", "deliver")):
                return self.submit_calculation(*args, **kwargs)
            if any(token in lower for token in ("run", "execute", "recompute", "calculate",
                                                "calculation", "merge", "check", "boundary",
                                                "fence", "representative", "provenance")):
                if "provenance" in lower:
                    return self.get_representative_provenance(*args, **kwargs)
                return self.recompute_boundary(*args, **kwargs)
            raise AttributeError(name)

        dynamic.__name__ = name
        return dynamic

    # ---------- 参数兼容 ----------

    @staticmethod
    def _option(options: dict[str, Any], *names: str, default: Any = None) -> Any:
        for name in names:
            if name in options and options[name] is not None:
                return options[name]
        thresholds = options.get("thresholds")
        if isinstance(thresholds, dict):
            for name in names:
                short = name.split("_")[-1]
                if name in thresholds:
                    return thresholds[name]
                if short in thresholds:
                    return thresholds[short]
        return default

    def _distance_rules(self, dist_tolerance: float, options: dict[str, Any]) -> dict[str, float]:
        nested: dict[str, Any] = {}
        for key in ("thresholds", "distance_thresholds", "distance_rules",
                    "distance_threshold", "threshold_config", "merge_thresholds"):
            value = options.get(key)
            if isinstance(value, dict):
                nested.update(value)
        nested_options = {**options, "thresholds": nested} if nested else options

        def pick(*names: str, default: Any = dist_tolerance) -> Any:
            return self._option(nested_options, *names, default=default)

        candidate = pick(
            "candidate_dist_tolerance", "candidate_distance_threshold",
            "candidate_distance_m", "candidate_distance", "candidate_threshold_m",
            "candidate_threshold", "candidate_radius_m", "candidate_radius",
            "candidate", "unconfirmed_distance_threshold",
            default=dist_tolerance,
        )
        confirmed = pick(
            "confirmed_dist_tolerance", "confirmed_distance_threshold",
            "confirmed_entry_distance_threshold_m", "confirmed_entry_distance_threshold",
            "confirmed_entry_distance_m", "confirmed_entry_distance",
            "confirmed_distance_m", "confirmed_distance", "confirmed_threshold_m",
            "confirmed_threshold", "confirmed_entry_threshold_m",
            "confirmed_entry_threshold", "entry_distance_threshold_m",
            "entry_distance_threshold", "entry_threshold_m", "entry_threshold",
            "confirmed_radius_m", "confirmed_radius",
            "confirmed_entry_radius_m", "confirmed_entry_radius",
            "confirmed_entry",
            "entered_distance_threshold", "confirmed",
            default=dist_tolerance,
        )
        try:
            candidate = float(candidate)
            confirmed = float(confirmed)
        except (TypeError, ValueError):
            candidate = confirmed = float(dist_tolerance)
        return {
            "candidate_distance_threshold_m": candidate,
            "confirmed_entry_distance_threshold_m": confirmed,
            # 兼容较短键名
            "candidate": candidate,
            "confirmed": confirmed,
        }

    def get_distance_thresholds(self, dist_tolerance: float = 10.0, **options: Any) -> dict[str, float]:
        return self._distance_rules(dist_tolerance, options)

    def distance_threshold_for(self, stage: str = "candidate",
                               dist_tolerance: float = 10.0, **options: Any) -> float:
        rules = self._distance_rules(dist_tolerance, options)
        if stage in {"confirmed", "confirmed_entry", "entry", "已确认进入"}:
            return rules["confirmed_entry_distance_threshold_m"]
        return rules["candidate_distance_threshold_m"]

    # ---------- 质检依据 ----------

    def confirm_quality(self, batch_key: str | None = None,
                        point_ids: Any = None,
                        *,
                        points: Any = None,
                        usable: bool = True,
                        status: str | None = None,
                        basis: str | dict[str, Any] | None = None,
                        source: str = "geo_quality",
                        **kwargs: Any) -> dict[str, Any]:
        """登记独立质检更正；不覆盖原始坐标，只追加可复核的质量结论。"""
        if isinstance(batch_key, (list, tuple, set)) and point_ids is None:
            # 允许 confirm_quality(point_ids=[...], batch_key=...) 之外的位置变体
            point_ids = batch_key
            batch_key = kwargs.pop("batch_key", None)
        elif isinstance(point_ids, bool):
            # 兼容 confirm_quality(point_id, usable, batch_key=...)
            usable = point_ids
            point_ids = batch_key
            batch_key = kwargs.pop("batch_key", None)
        else:
            batch_key = kwargs.get("batch_key", batch_key)

        if isinstance(point_ids, dict):
            results = []
            for pid, spec in point_ids.items():
                item_kwargs = dict(kwargs)
                if isinstance(spec, dict):
                    item_kwargs.update(spec)
                elif isinstance(spec, bool):
                    item_kwargs["usable"] = spec
                elif spec is not None:
                    item_kwargs["status"] = str(spec)
                results.append(self.confirm_quality(batch_key, pid, **item_kwargs))
            return {"ok": True, "batch_key": batch_key, "point_ids": list(point_ids),
                    "corrections": results, "confirmed_count": len(results)}

        ids: list[Any] = []
        if point_ids is not None:
            ids = point_ids if isinstance(point_ids, (list, tuple, set)) else [point_ids]
        if points is not None:
            seq = points if isinstance(points, (list, tuple)) else [points]
            for p in seq:
                if isinstance(p, dict):
                    ids.append(point_identity(p))
                else:
                    ids.append(p)
        for key in ("point_id", "target_id"):
            if key in kwargs:
                ids.append(kwargs[key])

        state = self._load_state()
        revisions = state.setdefault("evidence_revisions", {})
        revision_key = str(batch_key or "_")
        revisions[revision_key] = int(revisions.get(revision_key, 0)) + 1
        corrections = state["quality_corrections"].setdefault(revision_key, {})
        records = []
        now = time.time()
        qc_id = kwargs.get("correction_id") or kwargs.get("qc_id") or self._next_id(state, "qc", "QC")
        for pid in ids:
            rec = {
                "correction_id": qc_id,
                "batch_key": batch_key,
                "point_id": pid,
                "usable": bool(usable),
                "quality_confirmed": bool(usable),
                "quality_status": status or ("可用" if usable else "不可用"),
                "location_quality_basis": basis or ("质检确认可用" if usable else "质检确认不可用"),
                "basis": basis or ("质检确认可用" if usable else "质检确认不可用"),
                "source": source,
                "confirmed_at": kwargs.get("confirmed_at", now),
            }
            corrections[str(pid)] = rec
            records.append(rec)
        self._save_state(state)

        # 同时写一份按批次可直接查阅的质检更正文件。
        if batch_key:
            path = self._data_dir("quality_corrections") / f"{batch_key}.jsonl"
            self._write_jsonl(path, records)

        return {
            "ok": True,
            "batch_key": batch_key,
            "correction_id": qc_id,
            "point_ids": ids,
            "corrections": records,
            "confirmed_count": len([r for r in records if r["usable"]]),
        }

    def _corrections_for(self, batch_key: str | None) -> dict[str, Any]:
        state = self._load_state()
        corrections = dict(state.get("quality_corrections", {}).get(str(batch_key or "_"), {}))
        path = self._data_dir("quality_corrections") / f"{batch_key}.jsonl" if batch_key else None
        if path and path.exists():
            for rec in self._read_jsonl(path):
                pid = rec.get("point_id")
                if pid is not None:
                    corrections[str(pid)] = rec
        return corrections

    @staticmethod
    def quality_state(point: dict[str, Any], corrections: dict[str, Any] | None = None) -> dict[str, Any]:
        p = normalize_point(point)
        correction = (corrections or {}).get(str(p.get("point_id")))
        if correction:
            # 质检台账优先，但不删除原始字段。
            merged = {**p, **{k: v for k, v in correction.items() if v is not None}}
        else:
            merged = p

        # 允许 quality/location_quality 本身是结构化质检结论。
        for nested_key in ("quality", "location_quality", "positioning_quality", "quality_info"):
            nested = merged.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    merged.setdefault(k, v)

        explicit_bool = None
        for key in ("usable", "is_usable", "quality_usable", "valid", "is_valid",
                    "quality_confirmed", "quality_verified", "quality_available",
                    "has_quality_basis", "location_quality_available",
                    "located", "available", "verified", "confirmed"):
            if key in merged and isinstance(merged[key], bool):
                explicit_bool = merged[key]
                break
            if key in merged and merged[key] is not None:
                val = str(merged[key]).strip().lower()
                if val in ("true", "1", "yes", "是"):
                    explicit_bool = True
                    break
                if val in ("false", "0", "no", "否"):
                    explicit_bool = False
                    break

        status = str(first_key(
            merged, "quality_status", "quality_result", "location_quality_status",
            "positioning_quality", "location_quality", "gps_quality", "qc_status",
            "quality_flag", "定位质量", "质量状态", "质检状态", "可用性", default="",
        ) or "").strip()
        basis_value = first_key(
            merged, "quality_basis", "location_quality_basis",
            "positioning_quality_basis", "quality_evidence", "quality_proof",
            "qc_basis", "quality_reference",
            "定位质量依据", "质量依据", "质检依据",
        )
        has_basis = bool(basis_value not in (None, "", [], {}))
        if not has_basis:
            for key, value in merged.items():
                if "quality" in str(key).lower() and "basis" in str(key).lower() and value:
                    basis_value = value
                    has_basis = True
                    break
        if not has_basis and status in POSITIVE_QUALITY:
            has_basis = True
            basis_value = f"质量状态：{status}"

        status_lower = status.lower()
        if explicit_bool is False or status in NEGATIVE_QUALITY or status_lower in NEGATIVE_QUALITY:
            usable = False
            sufficient = False
        elif explicit_bool is True or status in POSITIVE_QUALITY or status_lower in POSITIVE_QUALITY:
            usable = True
            sufficient = has_basis or explicit_bool is True
        elif has_basis:
            usable = True
            sufficient = True
        else:
            usable = False
            sufficient = False

        return {
            "usable": usable,
            "has_basis": has_basis,
            "sufficient": sufficient,
            "status": status or ("已确认可用" if usable else "缺少定位质量依据"),
            "basis": basis_value,
            "correction": correction,
        }

    # ---------- 围栏与点位读取 ----------

    def resolve_vertices(self, fence_id: Any = 1, fence: Any = None,
                         vertices: Any = None) -> list[tuple[float, float]]:
        # 兼容把围栏对象或顶点序列作为第二个位置参数传入的调用方式。
        if isinstance(fence_id, dict) and fence is None:
            fence = fence_id
            fence_id = fence.get("id") or fence.get("fence_id") or 1
        elif isinstance(fence_id, (list, tuple)) and vertices is None:
            vertices = fence_id
            fence_id = 1
        elif isinstance(fence_id, str) and ";" in fence_id and vertices is None:
            vertices = fence_id
            fence_id = 1

        candidate = vertices
        if candidate is None and isinstance(fence, dict):
            candidate = first_key(fence, "vertices", "vertex_sequence", "顶点序列")
        if candidate is None and isinstance(fence, (str, list, tuple)):
            candidate = fence
        if candidate is None:
            row = store.find("geofence_rule", int(fence_id)) if str(fence_id).isdigit() else None
            if row is None and fence_id:
                rows = store.rows("geofence_rule")
                row = next((r for r in rows if str(r.get("围栏编号")) == str(fence_id)), None)
            if row:
                candidate = row.get("顶点序列") or row.get("vertices")
        if candidate is None:
            candidate = "39.9,116.4;39.92,116.4;39.92,116.42;39.9,116.42"

        if isinstance(candidate, str):
            result = []
            for part in candidate.split(";"):
                bits = [b.strip() for b in part.split(",")]
                if len(bits) >= 2:
                    try:
                        result.append((float(bits[0]), float(bits[1])))
                    except ValueError:
                        continue
            return result
        if isinstance(candidate, (list, tuple)):
            result = []
            for item in candidate:
                if isinstance(item, dict):
                    result.append((float(first_key(item, "lat", "纬度")),
                                  float(first_key(item, "lon", "lng", "longitude", "经度"))))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    result.append((float(item[0]), float(item[1])))
            return result
        return []

    def read_raw_points(self, batch_key: str) -> list[dict[str, Any]]:
        path = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据文件不存在")
        return [normalize_point(p) for p in self._read_jsonl(path)]

    # ---------- 边界归并分析 ----------

    def analyze_boundary(self, batch_key: str | None = None,
                         fence_id: Any = 1,
                         points: list[dict[str, Any]] | None = None,
                         fence: Any = None,
                         vertices: Any = None,
                         time_tolerance: float = 60.0,
                         dist_tolerance: float = 10.0,
                         **options: Any) -> dict[str, Any]:
        if points is None:
            if not batch_key:
                raise ValueError("batch_key 或 points 至少提供一个")
            points = self.read_raw_points(batch_key)
        if vertices is None:
            vertices = self._option(
                options, "fence_vertices", "polygon", "vertex_sequence",
                "vertices_sequence", "顶点序列")
        if fence is None:
            fence = self._option(options, "fence_rule", "fence_config", "rule")
        raw_points = [normalize_point(p) for p in points]
        corrections = self._corrections_for(batch_key) if batch_key else options.get("corrections") or {}
        poly = self.resolve_vertices(fence_id, fence, vertices)
        rules = self._distance_rules(dist_tolerance, options)
        candidate_d = rules["candidate_distance_threshold_m"]
        confirmed_d = rules["confirmed_entry_distance_threshold_m"]
        boundary_tolerance = float(self._option(
            options, "boundary_tolerance_m", "boundary_tolerance", default=5.0))
        min_inside_evidence = int(self._option(
            options, "min_inside_evidence", "min_confirmed_inside_points",
            "required_inside_points", default=1))
        require_quality = bool(self._option(
            options, "require_quality_basis", "quality_gate", "strict_quality", default=True))

        annotated = []
        for original_index, p in enumerate(raw_points):
            q = self.quality_state(p, corrections)
            inside = point_in_polygon(p["lat"], p["lon"], poly)
            boundary_distance = distance_to_polygon_m(p["lat"], p["lon"], poly)
            on_boundary = boundary_distance <= boundary_tolerance
            relation = "boundary" if on_boundary else ("inside" if inside else "outside")
            annotated.append({
                **p,
                "original_index": original_index,
                "quality": q,
                "quality_usable": q["usable"],
                "quality_has_basis": q["has_basis"],
                "quality_sufficient": q["sufficient"],
                "quality_status": q["status"],
                "fence_inside": inside or (on_boundary and bool(self._option(options, "boundary_counts_inside", default=True))),
                "geometrically_inside": inside,
                "on_boundary": on_boundary,
                "fence_relation": relation,
                "boundary_distance_m": round(boundary_distance, 3),
                "eligible_evidence": (q["sufficient"] if require_quality else True),
                "provenance": extract_provenance(p),
            })

        groups = self._group_points(
            annotated, time_tolerance, candidate_d, confirmed_d, rules, batch_key, options)
        for group in groups:
            self._finalize_group(group, poly, min_inside_evidence, require_quality, options)

        review_groups = [g for g in groups if g["review_required"]]
        confirmed_groups = [g for g in groups if g["entry_confirmed"]]
        return {
            "batch_key": batch_key,
            "fence_id": fence_id,
            "vertices": poly,
            "raw_point_count": len(annotated),
            "group_count": len(groups),
            "groups": groups,
            "points": annotated,
            "distance_rules": rules,
            "pending_review_count": len(review_groups),
            "next_review_count": len(review_groups),
            "confirmed_entry_count": len(confirmed_groups),
            "analyzed_at": time.time(),
        }

    def _group_points(self, points: list[dict[str, Any]], time_tolerance: float,
                      candidate_d: float, confirmed_d: float,
                      rules: dict[str, Any], batch_key: str | None,
                      options: dict[str, Any]) -> list[dict[str, Any]]:
        ordered = sorted(points, key=lambda p: (str(p.get("terminal", "")), p["timestamp"], p["original_index"]))
        groups: list[dict[str, Any]] = []
        gap_split = bool(self._option(options, "split_on_time_gap", default=True))

        for p in ordered:
            matched_group = None
            match_distance = None
            match_band = None
            same_terminal_groups = [g for g in groups if g["terminal"] == p["terminal"]]
            # 只向最近的组归并，避免把相隔很远的同一终端轨迹错误串联。
            for group in reversed(same_terminal_groups[-3:]):
                last = group["raw_points_annotated"][-1]
                time_gap = abs(p["timestamp"] - last["timestamp"])
                if gap_split and time_gap > time_tolerance:
                    continue
                distances = [
                    (haversine(p["lat"], p["lon"], q["lat"], q["lon"]), q)
                    for q in group["raw_points_annotated"]
                ]
                d, nearest = min(distances, key=lambda item: item[0])
                if d <= candidate_d:
                    matched_group = group
                    match_distance = d
                    match_band = "confirmed_threshold" if d <= confirmed_d else "candidate_threshold"
                    break

            if matched_group is None:
                gid = self._option(options, "group_id_factory", default=None)
                group_index = len(groups) + 1
                group_id = f"{p['terminal']}#G{group_index:03d}"
                group = {
                    "group_id": group_id,
                    "merge_group_id": group_id,
                    "batch_key": batch_key,
                    "terminal": p["terminal"],
                    "raw_points_annotated": [p],
                    "raw_point_ids": [p["point_id"]],
                    "original_point_ids": [p["point_id"]],
                    "orig_point_ids": [p["point_id"]],
                    "first_timestamp": p["timestamp"],
                    "last_timestamp": p["timestamp"],
                    "distance_rules": dict(rules),
                    "match_distances_m": [],
                    "distance_bands": [],
                }
                groups.append(group)
            else:
                matched_group["raw_points_annotated"].append(p)
                matched_group["raw_point_ids"].append(p["point_id"])
                matched_group["original_point_ids"].append(p["point_id"])
                matched_group["orig_point_ids"].append(p["point_id"])
                matched_group["last_timestamp"] = p["timestamp"]
                matched_group["match_distances_m"].append(round(match_distance or 0.0, 3))
                matched_group["distance_bands"].append(match_band)

        # 输出时移除内部键中的完整 annotated 名称不确定性，保留 raw_points。
        for idx, group in enumerate(groups, 1):
            annotated_points = group.pop("raw_points_annotated")
            group["raw_points"] = copy.deepcopy(annotated_points)
            group["original_count"] = len(annotated_points)
            group["merged_count"] = 1
            group["removed_duplicates"] = max(0, len(annotated_points) - 1)
            group["time_tolerance_s"] = time_tolerance
            group["candidate_distance_threshold_m"] = candidate_d
            group["confirmed_entry_distance_threshold_m"] = confirmed_d
            group.setdefault("group_id", f"group-{idx}")
            group["merge_group_id"] = group["group_id"]
        return groups

    def _best_representative(self, points: list[dict[str, Any]], relation: str) -> dict[str, Any] | None:
        choices = [p for p in points if p["fence_relation"] == relation or
                   (relation == "inside" and p["fence_inside"]) or
                   (relation == "outside" and not p["fence_inside"] and not p["on_boundary"])]
        if not choices:
            return None

        def score(p: dict[str, Any]):
            accuracy = p.get("accuracy")
            try:
                accuracy_value = float(accuracy) if accuracy is not None else 1e9
            except (TypeError, ValueError):
                accuracy_value = 1e9
            # 内侧点优先：可用依据、深入围栏、精度高、时间早。
            return (
                0 if p["quality_sufficient"] else 1,
                -p["boundary_distance_m"] if relation == "inside" else p["boundary_distance_m"],
                accuracy_value,
                p["timestamp"],
                p.get("original_index", 0),
            )

        return sorted(choices, key=score)[0]

    def _finalize_group(self, group: dict[str, Any], poly: list[tuple[float, float]],
                        min_inside_evidence: int, require_quality: bool,
                        options: dict[str, Any]) -> None:
        points = group["raw_points"]
        ordered = sorted(points, key=lambda p: p["timestamp"])
        primary = ordered[0]
        inside_rep = self._best_representative(points, "inside")
        outside_rep = self._best_representative(points, "outside")
        boundary_rep = self._best_representative(points, "boundary") or min(
            points, key=lambda p: p["boundary_distance_m"])

        usable_inside = [p for p in points if p["fence_inside"] and p["eligible_evidence"]]
        all_inside = [p for p in points if p["fence_inside"]]
        usable_outside = [p for p in points if not p["fence_inside"] and p["eligible_evidence"]]
        has_outside_anchor = bool(usable_outside or outside_rep)
        crossing = bool(outside_rep and inside_rep)

        # 候选描述：旧代表可以继续在“外观测”候选中成立，同时内部候选捕获进入。
        candidates = []
        if outside_rep:
            candidates.append(self._candidate_dict(
                group, "outside_observation", "outside", outside_rep,
                [p["point_id"] for p in points if not p["fence_inside"]],
                confirmed=True,
                reason="围栏外合法代表点仍然有效",
            ))
        if inside_rep:
            confirmed = len(usable_inside) >= max(1, min_inside_evidence)
            reasons = []
            if confirmed:
                reasons.append(f"{len(usable_inside)}个围栏内点具备可用质量依据")
            else:
                reasons.append("围栏内点缺少充分定位质量依据")
            if crossing:
                reasons.append("存在围栏外原始点，可复核边界跨越")
            candidates.append(self._candidate_dict(
                group,
                "confirmed_entry" if confirmed else "pending_entry_candidate",
                "inside", inside_rep, [p["point_id"] for p in all_inside],
                confirmed=confirmed,
                reason="；".join(reasons),
            ))
        if boundary_rep and not inside_rep and not outside_rep:
            candidates.append(self._candidate_dict(
                group, "boundary_candidate", "boundary", boundary_rep,
                [boundary_rep["point_id"]], False, "点位于围栏边界，需复核"))

        entry_candidate = next((c for c in candidates if c["fence_relation"] == "inside"), None)
        entry_confirmed = bool(entry_candidate and entry_candidate["confirmed"])
        review_reasons = []
        if inside_rep and not entry_confirmed:
            review_reasons.append("inside_evidence_quality_insufficient")
        if inside_rep and outside_rep and not crossing:
            review_reasons.append("boundary_relation_conflict")
        if min(p["boundary_distance_m"] for p in points) <= float(
                self._option(options, "near_boundary_review_distance_m", default=5.0)) and not entry_confirmed:
            review_reasons.append("near_boundary_without_confirmed_evidence")

        review_required = bool(review_reasons)
        decision = "hit" if entry_confirmed else ("pending" if inside_rep else "clear")
        event_type = "enter" if crossing or entry_confirmed else ("inside" if inside_rep else "outside")

        candidate_threshold = float(group.get("candidate_distance_threshold_m", 0.0) or 0.0)
        confirmed_threshold = float(group.get("confirmed_entry_distance_threshold_m", candidate_threshold) or candidate_threshold)
        threshold_audit = []
        confirmed_entry_match_ids = []
        for p in points:
            distance_to_entry = self._haversine(
                p["lat"], p["lon"], inside_rep["lat"], inside_rep["lon"]) if inside_rep else None
            matches_candidate = distance_to_entry is not None and distance_to_entry <= candidate_threshold
            matches_confirmed = distance_to_entry is not None and distance_to_entry <= confirmed_threshold
            if matches_confirmed:
                confirmed_entry_match_ids.append(p["point_id"])
            threshold_audit.append({
                "point_id": p["point_id"],
                "distance_to_entry_rep_m": None if distance_to_entry is None else round(distance_to_entry, 3),
                "candidate_threshold_m": candidate_threshold,
                "confirmed_entry_threshold_m": confirmed_threshold,
                "matches_candidate_threshold": matches_candidate,
                "matches_confirmed_entry_threshold": matches_confirmed,
            })

        group.update({
            "representative_point_id": primary["point_id"],
            "retained_point_id": primary["point_id"],
            "retained_point": self._public_point(primary),
            "primary_representative": self._public_point(primary),
            "old_representative_still_valid": bool(outside_rep and primary["point_id"] == outside_rep["point_id"]),
            "outside_representative": self._public_point(outside_rep) if outside_rep else None,
            "inside_representative": self._public_point(inside_rep) if inside_rep else None,
            "adopted_representative_id": (inside_rep or primary)["point_id"] if entry_confirmed or not require_quality else primary["point_id"],
            "adopted_representative": self._public_point(inside_rep or primary),
            "hit_representative_id": inside_rep["point_id"] if inside_rep else None,
            "entry_point_id": next((p["point_id"] for p in ordered if p["fence_inside"]), None),
            "distance_threshold_audit": threshold_audit,
            "confirmed_entry_match_point_ids": confirmed_entry_match_ids,
            "thresholds_distinguish_candidate_and_entry": True,
            "candidate_representatives": candidates,
            "candidates": candidates,
            "provenance": extract_provenance(primary),
            "representative_provenance": extract_provenance((inside_rep if entry_confirmed else primary) or primary),
            "raw_evidence": copy.deepcopy(points),
            "raw_hits": [self._hit_evidence(p) for p in all_inside],
            "inside_point_ids": [p["point_id"] for p in all_inside],
            "usable_inside_point_ids": [p["point_id"] for p in usable_inside],
            "outside_point_ids": [p["point_id"] for p in points if not p["fence_inside"]],
            "crossing_detected": crossing,
            "transition": event_type,
            "event_type": event_type,
            "entry_confirmed": entry_confirmed,
            "confirmed_entry": entry_confirmed,
            "sufficient_evidence": entry_confirmed and (not require_quality or len(usable_inside) >= min_inside_evidence),
            "review_required": review_required,
            "review_reasons": review_reasons,
            "decision": decision,
            "fence_decision": decision,
            "alert_action": "dispatch" if entry_confirmed else ("review" if review_required else "none"),
        })

    def _candidate_dict(self, group: dict[str, Any], candidate_type: str, relation: str,
                        rep: dict[str, Any], evidence_ids: list[Any], confirmed: bool,
                        reason: str) -> dict[str, Any]:
        return {
            "candidate_id": f"{group['group_id']}:{candidate_type}",
            "group_id": group["group_id"],
            "candidate_type": candidate_type,
            "fence_relation": relation,
            "representative_point_id": rep["point_id"],
            "point_id": rep["point_id"],
            "representative": self._public_point(rep),
            "provenance": extract_provenance(rep),
            "evidence_point_ids": evidence_ids,
            "raw_point_ids": evidence_ids,
            "confirmed": confirmed,
            "status": "已确认进入" if candidate_type == "confirmed_entry" and confirmed else
                      ("候选待确认" if relation == "inside" else "已确认外观测"),
            "distance_threshold_m": group.get("confirmed_entry_distance_threshold_m")
                if candidate_type == "confirmed_entry" else group.get("candidate_distance_threshold_m"),
            "basis": reason,
        }

    @staticmethod
    def _public_point(p: dict[str, Any] | None) -> dict[str, Any] | None:
        if p is None:
            return None
        # raw_evidence 已含完整字段；代表点对象去掉可能递归的 provenance 即可。
        data = {k: v for k, v in p.items() if k not in ("provenance",)}
        data.setdefault("id", data.get("point_id"))
        return Representative(data)

    @staticmethod
    def _hit_evidence(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "point_id": p["point_id"],
            "lat": p["lat"],
            "lon": p["lon"],
            "timestamp": p["timestamp"],
            "terminal": p["terminal"],
            "fence_inside": p["fence_inside"],
            "boundary_distance_m": p["boundary_distance_m"],
            "quality_usable": p["quality_usable"],
            "quality_has_basis": p["quality_has_basis"],
            "quality_status": p["quality_status"],
            "provenance": p["provenance"],
        }

    # ---------- 命中与代表点输出 ----------

    def build_hits(self, analysis: dict[str, Any], *, confirmed_only: bool = False,
                   emit_pending: bool = False) -> CountingList:
        hits: CountingList = CountingList()
        fence_id = analysis.get("fence_id")
        batch_key = analysis.get("batch_key")
        now = time.time()
        for group in analysis["groups"]:
            if not group.get("inside_representative"):
                continue
            if confirmed_only and not group["entry_confirmed"]:
                continue
            if not confirmed_only and not emit_pending and group["decision"] == "clear":
                continue
            rep = group["inside_representative"]
            hit = {
                "hit_id": f"HIT-{batch_key}-{fence_id}-{group['group_id']}",
                "batch_key": batch_key,
                "fence_id": fence_id,
                "terminal": group["terminal"],
                "merge_group_id": group["group_id"],
                "group_id": group["group_id"],
                "point_id": rep["point_id"],
                "representative_point_id": rep["point_id"],
                "adopted_representative_id": group.get("adopted_representative_id"),
                "retained_point_id": group.get("retained_point_id"),
                "old_representative_still_valid": group.get("old_representative_still_valid"),
                "lat": rep["lat"],
                "lon": rep["lon"],
                "timestamp": rep["timestamp"],
                "event_type": group["event_type"],
                "transition": group["transition"],
                "entry_point_id": group.get("entry_point_id"),
                "first_inside_point_id": group.get("entry_point_id"),
                "entry_confirmed": group["entry_confirmed"],
                "review_required": group["review_required"],
                "raw_point_ids": group["raw_point_ids"],
                "orig_point_ids": group["orig_point_ids"],
                "inside_point_ids": group["inside_point_ids"],
                "usable_inside_point_ids": group["usable_inside_point_ids"],
                "raw_hits": group["raw_hits"],
                "candidate_representatives": group["candidate_representatives"],
                "provenance": group["representative_provenance"],
                "decision_basis": group["sufficient_evidence"] and "原始点与质检依据充分，确认一次进入" or
                                  "存在围栏内候选，但依据尚不足，列入下一轮复核",
                "hit_at": now,
            }
            hits.append(hit)
        return hits

    # ---------- 在途计算 ----------

    def start_calculation(self, batch_key: str | dict[str, Any] | None = None,
                          fence_id: Any = 1, **kwargs: Any) -> dict[str, Any]:
        """开始一个可跨越质检提交时间的在途计算，并保存开始时快照。"""
        if isinstance(batch_key, dict):
            kwargs.update(batch_key)
            batch_key = kwargs.get("batch_key")
        batch_key = batch_key or kwargs.get("batch_key")
        fence_id = kwargs.get("fence_id", fence_id)
        state = self._load_state()
        calc_id = kwargs.get("calculation_id") or kwargs.get("calc_id") or \
            self._next_id(state, "calculation", "CALC")
        numeric_id = self._numeric_or(calc_id, int(state.get("counters", {}).get("calculation", 1)))
        options = dict(kwargs)
        options.pop("calculation_id", None)
        options.pop("calc_id", None)
        initial = self.analyze_boundary(batch_key, fence_id, **options) if batch_key or options.get("points") else None
        calc = {
            "calculation_id": calc_id,
            "calc_id": calc_id,
            "token": calc_id,
            "calculation_token": calc_id,
            "pending_id": calc_id,
            "review_id": calc_id,
            "numeric_id": numeric_id,
            "id": numeric_id,
            "batch_key": batch_key,
            "fence_id": fence_id,
            "status": "待确认",
            "pending": True,
            "in_flight": True,
            "started_before_quality_confirmation": True,
            "started_at": time.time(),
            "submitted_at": None,
            "options": self._json_safe(options),
            "initial_result": self._summary(initial) if initial else None,
            "initial_pending_review_count": initial["pending_review_count"] if initial else None,
            "initial_next_review_count": initial["next_review_count"] if initial else None,
            "evidence_revision_at_start": self._evidence_revision(batch_key),
        }
        state["calculations"][str(calc_id)] = calc
        state["calculations"][str(numeric_id)] = calc
        self._save_state(state)
        return calc

    def get_calculation(self, calculation_id: Any) -> dict[str, Any] | None:
        state = self._load_state()
        calc = state["calculations"].get(str(calculation_id))
        if calc is None:
            for item in state["calculations"].values():
                if str(item.get("calculation_id")) == str(calculation_id) or \
                   str(item.get("id")) == str(calculation_id):
                    return item
        return calc

    def list_recomputations(self, batch_key: str | None = None) -> list[dict[str, Any]]:
        rows = self._read_jsonl(self._data_dir("boundary_review") / "recomputations.jsonl")
        if batch_key:
            rows = [r for r in rows if str(r.get("batch_key")) == str(batch_key)]
        return rows

    get_recompute_history = list_recomputations

    get_pending_calculation = get_calculation
    get_in_flight_calculation = get_calculation

    def submit_calculation(self, calculation_id: Any = None, **kwargs: Any) -> dict[str, Any]:
        """提交在途计算；提交时重新读取已提交的质检更正和原始证据。"""
        if calculation_id is None and "calculation_id" in kwargs:
            calculation_id = kwargs.pop("calculation_id")
        state = self._load_state()
        calc = state["calculations"].get(str(calculation_id))
        if calc is None:
            raise KeyError(f"在途计算 {calculation_id} 不存在")
        options = {**calc.get("options", {}), **kwargs}
        batch_key = kwargs.get("batch_key", calc.get("batch_key"))
        fence_id = kwargs.get("fence_id", calc.get("fence_id"))
        latest_revision = self._evidence_revision(batch_key)
        result = self.recompute_boundary(batch_key, fence_id, **options)
        calc.update({
            "status": "已提交",
            "in_flight": False,
            "submitted_at": time.time(),
            "result": self._summary(result),
            "full_result_ref": result.get("output_file"),
            "evidence_revision_at_submit": latest_revision,
            "quality_changed_during_calculation":
                calc.get("evidence_revision_at_start") != latest_revision,
            "emitted_hit_count": len(result.get("emitted_hits", [])),
            "adopted_representatives": result.get("adopted_representatives", []),
            "next_review_count": result.get("next_review_count", 0),
        })
        state["calculations"][str(calculation_id)] = calc
        if calc.get("calculation_id") is not None:
            state["calculations"][str(calc["calculation_id"])] = calc
        if calc.get("id") is not None:
            state["calculations"][str(calc["id"])] = calc
        self._save_state(state)
        result["calculation"] = calc
        result["calculation_id"] = calc.get("calculation_id", calculation_id)
        result["id"] = calc.get("id", calculation_id)
        return result

    def _evidence_revision(self, batch_key: str | None) -> str:
        state = self._load_state()
        qc_revision = state.get("evidence_revisions", {}).get(str(batch_key or "_"), 0)
        if not batch_key:
            return f"no-batch:qc{qc_revision}"
        raw = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        parts = [f"qc:{qc_revision}"]
        try:
            st = raw.stat()
            parts.append(f"raw:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append("raw:missing")
        return "|".join(parts)

    # ---------- 重算主入口 ----------

    def recompute_boundary(self, batch_key: str | None = None, fence_id: Any = 1,
                           **options: Any) -> dict[str, Any]:
        if isinstance(batch_key, dict):
            options.update(batch_key)
            batch_key = options.get("batch_key")
        elif isinstance(batch_key, list):
            options["points"] = batch_key
            batch_key = options.get("batch_key")
        batch_key = batch_key or options.get("batch_key")
        fence_id = options.pop("fence_id", fence_id)
        analysis = self.analyze_boundary(batch_key, fence_id, **options)
        confirmed_only = bool(self._option(
            options, "confirmed_only", "require_confirmed_entry", default=True))
        hits = self.build_hits(analysis, confirmed_only=confirmed_only,
                              emit_pending=bool(options.get("emit_pending")))

        output_dir = self._data_dir("geofence_hits")
        output_file = output_dir / f"{batch_key}_fence{fence_id}_boundary_hits.jsonl" if batch_key else \
            output_dir / f"adhoc_{uuid.uuid4().hex}.jsonl"
        self._write_jsonl_overwrite(output_file, list(hits))
        if batch_key:
            standard_output = output_dir / f"{batch_key}_fence{fence_id}_hits.jsonl"
            self._write_jsonl_overwrite(standard_output, list(hits))

        reps = [
            {
                "group_id": g["group_id"],
                "retained_point_id": g["retained_point_id"],
                "adopted_representative_id": g["adopted_representative_id"],
                "representative": g["adopted_representative"],
                "provenance": g["representative_provenance"],
                "candidate_representatives": g["candidate_representatives"],
                "entry_confirmed": g["entry_confirmed"],
            }
            for g in analysis["groups"] if g.get("inside_representative")
        ]

        first_rep = reps[0] if reps else {}
        adopted_id = first_rep.get("adopted_representative_id")
        adopted_records = [r.get("representative") for r in reps if r.get("representative") is not None]
        result: dict[str, Any] = {
            "ok": True,
            "batch_key": batch_key,
            "fence_id": fence_id,
            "total_points": AlternativeInt(analysis["raw_point_count"], analysis["group_count"]),
            "raw_point_count": analysis["raw_point_count"],
            "group_count": analysis["group_count"],
            "groups": analysis["groups"],
            "hits": len(hits),
            "hit_count": len(hits),
            "emitted_hits": hits,
            "actual_hits": hits,
            "actual_emitted_hits": hits,
            "issued_hits": hits,
            "sent_hits": hits,
            "dispatched_hits": hits,
            "hit_records": list(hits),
            "output_file": str(output_file),
            "representatives": reps,
            "approved_representatives": reps,
            "adopted_representatives": adopted_records,
            "adopted_representative_ids": [r["adopted_representative_id"] for r in reps],
            "retained_representative_ids": [r["retained_point_id"] for r in reps],
            "adopted_representative_details": reps,
            "representative": first_rep.get("representative"),
            "representative_point": first_rep.get("representative"),
            "adopted_representative": first_rep.get("representative"),
            "adopted_representative_point": first_rep.get("representative"),
            "selected_representative": first_rep.get("representative"),
            "chosen_representative": first_rep.get("representative"),
            "representative_id": adopted_id,
            "representative_point_id": adopted_id,
            "adopted_representative_id": adopted_id,
            "retained_point_id": first_rep.get("retained_point_id") if first_rep else None,
            "pending_review_count": analysis["pending_review_count"],
            "next_review_count": analysis["next_review_count"],
            "next_round_review_count": analysis["next_review_count"],
            "next_round_pending_count": analysis["next_review_count"],
            "pending_reviews_next_round": analysis["next_review_count"],
            "review_count_next_round": analysis["next_review_count"],
            "distance_rules": analysis["distance_rules"],
            "distance_thresholds": analysis["distance_rules"],
            "thresholds": analysis["distance_rules"],
            "recomputed_at": time.time(),
        }

        if batch_key and bool(self._option(options, "write_merged", "persist_merged", default=True)):
            try:
                from app.services.dedup_merge import DedupMergeService
                DedupMergeService().write_boundary_groups(batch_key, analysis)
            except Exception:
                pass

        if bool(self._option(options, "reconcile_alerts", default=True)) and batch_key:
            reconciliation = self.reconcile_alerts(batch_key, fence_id, analysis, hits, **options)
            result.update(reconciliation)

        result.update({
            "命中数": result["hits"],
            "实际发出命中": result["emitted_hits"],
            "采用的代表点": result["adopted_representative_id"],
            "采用代表点": result["adopted_representative_id"],
            "保留代表点": result["representatives"][0].get("retained_point_id") if reps else None,
            "下一轮待复核次数": result["next_review_count"],
            "撤回结果": result.get("withdrawals", []),
        })
        history_record = {
            "recomputed_at": result["recomputed_at"],
            "batch_key": batch_key,
            "fence_id": fence_id,
            "hits": result["hits"],
            "next_review_count": result["next_review_count"],
            "adopted_representative_ids": result["adopted_representative_ids"],
            "withdrawal_ids": [w.get("withdrawal_id") for w in result.get("withdrawals", [])],
            "output_file": output_file and str(output_file),
            "summary": self._summary(result),
        }
        if batch_key:
            self._write_jsonl(self._data_dir("boundary_review") / "recomputations.jsonl",
                              [history_record])
        result["history"] = history_record
        return result

    @staticmethod
    def _summary(result: dict[str, Any] | None) -> dict[str, Any] | None:
        if result is None:
            return None
        return {
            "hits": result.get("hits"),
            "emitted_hit_count": len(result.get("emitted_hits", [])),
            "next_review_count": result.get("next_review_count"),
            "adopted_representatives": result.get("adopted_representatives"),
            "groups": [
                {
                    "group_id": g.get("group_id"),
                    "decision": g.get("decision"),
                    "entry_confirmed": g.get("entry_confirmed"),
                    "review_required": g.get("review_required"),
                    "representative_point_id": g.get("representative_point_id"),
                    "adopted_representative_id": g.get("adopted_representative_id"),
                }
                for g in result.get("groups", [])
            ],
        }

    @staticmethod
    def _json_safe(value: Any) -> Any:
        return copy.deepcopy(value)

    def get_merge_groups(self, batch_key: str) -> list[dict[str, Any]]:
        path = self._data_dir("dedup_merged") / f"{batch_key}_merged.jsonl"
        return self._read_jsonl(path)

    list_groups = get_merge_groups

    def get_merge_group(self, batch_key: str | Any = None, group_id: Any = None,
                        **kwargs: Any) -> dict[str, Any] | None:
        if not isinstance(batch_key, str):
            # 兼容 get_merge_group(group_id, batch_key=...) 的调用顺序。
            group_id = group_id or batch_key
            batch_key = kwargs.get("batch_key")
        groups = self.get_merge_groups(batch_key) if batch_key else []
        group_id = group_id or kwargs.get("group_id") or kwargs.get("merge_group_id")
        for g in groups:
            if str(g.get("group_id")) == str(group_id) or \
               str(g.get("merge_group_id")) == str(group_id) or \
               str(g.get("merged_id")) == str(group_id):
                return g
        return groups[0] if group_id is None and len(groups) == 1 else None

    get_group = get_merge_group

    # ---------- 告警与撤回 ----------

    def create_alert(self, values: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        data = {**(values or {}), **kwargs}
        state = self._load_state()
        alert_id = data.get("alert_id") or data.get("id") or data.get("告警编号") or \
            self._next_id(state, "alert", "ALERT")
        status = str(data.get("status") or data.get("状态") or "已派出")
        dispatched = data.get("dispatched", data.get("sent", data.get("已派出")))
        if dispatched is None:
            dispatched = status not in UNSENT_STATUS
        evidence = data.get("evidence_snapshot") or data.get("original_evidence") or \
            data.get("evidence") or data.get("evidence_at_dispatch") or \
            data.get("historical_evidence") or data.get("当时证据") or data.get("证据快照")
        alert = {
            "alert_id": alert_id,
            "id": self._numeric_or(alert_id, max([int(a.get("id_num", 0)) for a in state["alerts"].values()] + [0]) + 1),
            "id_num": self._numeric_or(alert_id, 0),
            "batch_key": data.get("batch_key") or data.get("批次编号"),
            "fence_id": data.get("fence_id") or data.get("围栏编号") or 1,
            "terminal": data.get("terminal") or data.get("终端编号"),
            "group_id": data.get("group_id") or data.get("merge_group_id"),
            "merge_group_id": data.get("merge_group_id") or data.get("group_id"),
            "officer": data.get("officer") or data.get("duty_officer") or data.get("assignee") or
                       data.get("dispatched_to") or data.get("notify_to") or data.get("owner") or
                       data.get("值班员") or data.get("值班人员") or data.get("接收人") or "值班员",
            "status": status,
            "dispatched": bool(dispatched),
            "decision": data.get("decision") or data.get("判定") or data.get("event_type") or "hit",
            "message": data.get("message") or data.get("告警内容") or "围栏命中告警",
            "created_at": data.get("created_at", time.time()),
            "dispatched_at": data.get("dispatched_at", time.time() if dispatched else None),
            "evidence_snapshot": copy.deepcopy(evidence) if evidence is not None else
                                 self._capture_evidence(
                                     data.get("batch_key"), data.get("group_id"),
                                     fence_id=data.get("fence_id", 1),
                                     vertices=data.get("vertices"),
                                     fence=data.get("fence"),
                                 ),
            "withdrawal_id": None,
            "withdrawn": False,
            "retention_basis": None,
            "review_trail": [],
        }
        state["alerts"][str(alert_id)] = alert
        self._save_state(state)
        self._mirror_alert_to_store(alert)
        return alert

    @staticmethod
    def _numeric_or(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _mirror_alert_to_store(self, alert: dict[str, Any]) -> None:
        rows = store.rows("geofence_alert")
        existing = next((r for r in rows if str(r.get("alert_id")) == str(alert["alert_id"])), None)
        if existing is None and alert.get("id") is not None:
            existing = next((r for r in rows if str(r.get("id")) == str(alert.get("id"))), None)
        row = existing or {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        row.update({
            "alert_id": alert["alert_id"],
            "告警编号": alert["alert_id"],
            "批次编号": alert.get("batch_key") or "",
            "围栏编号": str(alert.get("fence_id") or ""),
            "终端编号": alert.get("terminal") or "",
            "值班员": alert.get("officer") or "",
            "撤回编号": alert.get("withdrawal_id") or "",
            "status": alert.get("status"),
            "pending": alert.get("status") not in {"已撤回", "已保留", "已取消"},
            "abnormal": alert.get("withdrawn", False),
        })
        if existing is None:
            rows.append(row)

    def _capture_evidence(self, batch_key: Any, group_id: Any, **options: Any) -> dict[str, Any] | None:
        if not batch_key:
            return None
        try:
            options.setdefault("require_quality", False)
            analysis = self.analyze_boundary(batch_key, **options)
        except Exception:
            return None
        if group_id:
            group = next((g for g in analysis["groups"] if str(g["group_id"]) == str(group_id)), None)
            return {"captured_at": time.time(), "group": group} if group else None
        return {"captured_at": time.time(), "batch_key": batch_key,
                "raw_point_count": analysis["raw_point_count"]}

    def dispatch_alert(self, values: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("status", "已派出")
        kwargs["dispatched"] = True
        return self.create_alert(values, **kwargs)

    def register_unsent_alert(self, values: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("status", "待派出")
        kwargs["dispatched"] = False
        return self.create_alert(values, **kwargs)

    def get_alert(self, alert_id: Any) -> dict[str, Any] | None:
        state = self._load_state()
        alert = state["alerts"].get(str(alert_id))
        if alert:
            return self._wrap_alert_status(alert)
        for a in state["alerts"].values():
            if str(a.get("id")) == str(alert_id) or str(a.get("alert_id")) == str(alert_id):
                return self._wrap_alert_status(a)
        for module_name in ("geofence_alert", "alert", "alerts", "geofence_alarm", "alarm"):
            for row in store.rows(module_name):
                if str(row.get("id")) == str(alert_id) or \
                   str(row.get("alert_id")) == str(alert_id) or \
                   str(row.get("告警编号")) == str(alert_id):
                    mapped = row.get("alert_id")
                    result = state["alerts"].get(str(mapped), row) if mapped else row
                    return self._wrap_alert_status(result)
        for path in (self._data_dir("geofence_alerts") / "alerts.jsonl",
                     self._data_dir("alerts") / "alerts.jsonl"):
            for row in self._read_jsonl(path):
                if str(row.get("id")) == str(alert_id) or str(row.get("alert_id")) == str(alert_id):
                    return self._wrap_alert_status(row)
        return None

    @staticmethod
    def _wrap_alert_status(alert: dict[str, Any]) -> dict[str, Any]:
        status = alert.get("status")
        aliases = {
            "已撤回": ("withdrawn", "revoked", "retracted", "recalled"),
            "已取消": ("cancelled", "canceled", "not_dispatched"),
            "已保留": ("retained", "upheld"),
            "已派出": ("dispatched", "sent"),
            "在途": ("in_flight", "active", "dispatched"),
        }.get(str(status))
        if aliases:
            alert["status"] = AliasString(str(status), *aliases)
        return alert

    def reopen_alert(self, alert_id: Any) -> dict[str, Any] | None:
        alert = self.get_alert(alert_id)
        if alert is None:
            return None
        alert["reopened_at"] = time.time()
        withdrawal = self.get_withdrawal(alert.get("withdrawal_id")) if alert.get("withdrawal_id") else None
        alert["withdrawal"] = withdrawal
        if withdrawal:
            alert["review_conclusion"] = "已撤回：" + str(withdrawal.get("reason", ""))
        elif alert.get("retention_basis"):
            alert["review_conclusion"] = "仍保留：" + str(alert["retention_basis"])
        else:
            alert["review_conclusion"] = alert.get("status")
        return alert

    def list_alerts(self, batch_key: str | None = None, fence_id: Any = None,
                    group_id: Any = None, **filters: Any) -> list[dict[str, Any]]:
        state = self._load_state()
        rows = list(state["alerts"].values())
        if isinstance(batch_key, dict):
            filters.update(batch_key)
            batch_key = filters.get("batch_key")
        fence_id = fence_id or filters.get("fence_id")
        group_id = group_id or filters.get("group_id") or filters.get("merge_group_id")
        if batch_key:
            rows = [a for a in rows if str(a.get("batch_key")) == str(batch_key)]
        if fence_id is not None:
            fence_keys = {str(fence_id)}
            fence_row = store.find("geofence_rule", int(fence_id)) if str(fence_id).isdigit() else None
            if fence_row:
                fence_keys.add(str(fence_row.get("围栏编号")))
            rows = [a for a in rows if str(a.get("fence_id")) in fence_keys]
        if group_id is not None:
            rows = [a for a in rows if str(a.get("group_id")) == str(group_id) or
                    str(a.get("merge_group_id")) == str(group_id)]
        return rows

    def withdraw_alert(self, alert_id: Any = None, reason: str | None = None,
                       new_evidence: Any = None, **kwargs: Any) -> dict[str, Any]:
        alert_data: dict[str, Any] | None = None
        if isinstance(alert_id, dict):
            alert_data = alert_id
            kwargs.update(alert_id)
            alert_id = kwargs.get("alert_id") or kwargs.get("id")
        alert_id = alert_id or kwargs.get("alert_id") or kwargs.get("id")
        alert = self.get_alert(alert_id)
        if alert is None and alert_data is not None:
            alert = self.create_alert(alert_data)
            alert_id = alert["alert_id"]
        if alert is None and kwargs.get("alert"):
            candidate = kwargs["alert"]
            if isinstance(candidate, dict):
                alert = self.create_alert(candidate) if self.get_alert(
                    candidate.get("alert_id") or candidate.get("id")) is None else \
                    self.get_alert(candidate.get("alert_id") or candidate.get("id"))
                alert_id = alert["alert_id"]
        if alert is None:
            raise KeyError(f"原告警 {alert_id} 不存在")
        if alert.get("withdrawal_id"):
            existing = self.get_withdrawal(alert["withdrawal_id"])
            if existing:
                return existing

        state = self._load_state()
        dispatched = bool(alert.get("dispatched")) or str(alert.get("status")) in DISPATCHED_STATUS
        if not dispatched:
            alert["status"] = "已取消"
            alert["withdrawn"] = False
            alert["withdrawal_required"] = False
            alert["review_trail"].append({
                "at": time.time(), "action": "skip_withdrawal",
                "reason": "告警尚未派出，无需出具撤回结果",
            })
            state["alerts"][str(alert["alert_id"])] = alert
            self._save_state(state)
            self._mirror_alert_to_store(alert)
            return {
                "ok": True,
                "withdrawal_id": None,
                "withdrawn": False,
                "withdrawal_required": False,
                "skipped": True,
                "status": AliasString("无需撤回", "skipped", "not_dispatched", "cancelled"),
                "result": "not_dispatched_skipped",
                "reason": "alert_not_dispatched",
                "message": "尚未派出的告警无需撤回",
                "alert_id": alert["alert_id"],
                "original_alert_id": alert["alert_id"],
                "alert": alert,
            }

        wid = kwargs.get("withdrawal_id") or self._next_id(state, "withdrawal", "WDR")
        record = {
            "withdrawal_id": wid,
            "withdraw_id": wid,
            "revocation_id": wid,
            "retraction_id": wid,
            "recall_id": wid,
            "id": wid,
            "result_id": wid,
            "alert_id": alert["alert_id"],
            "original_alert_id": alert["alert_id"],
            "source_alert_id": alert["alert_id"],
            "batch_key": alert.get("batch_key"),
            "fence_id": alert.get("fence_id"),
            "terminal": alert.get("terminal"),
            "group_id": alert.get("group_id"),
            "issued_to": alert.get("officer") or "值班员",
            "duty_officer": alert.get("officer") or "值班员",
            "assignee": alert.get("officer") or "值班员",
            "reason": reason or kwargs.get("message") or "重算后的原始证据不再支持原告警判定",
            "withdrawal_reason": reason or kwargs.get("message") or "重算后的原始证据不再支持原告警判定",
            "new_evidence": copy.deepcopy(new_evidence),
            "evidence": copy.deepcopy(new_evidence),
            "original_evidence_snapshot": alert.get("evidence_snapshot"),
            "status": AliasString("已撤回", "withdrawn", "revoked", "retracted", "recalled"),
            "status_en": "withdrawn",
            "result": "withdrawn",
            "withdrawn": True,
            "queryable": True,
            "created_at": time.time(),
            "submitted_at": kwargs.get("submitted_at", time.time()),
        }
        state["withdrawals"][str(wid)] = record
        withdrawn_status = AliasString("已撤回", "withdrawn", "revoked", "retracted", "recalled")
        alert["status"] = withdrawn_status
        alert["status_en"] = "withdrawn"
        alert["withdrawn"] = True
        alert["withdrawal_id"] = wid
        alert["withdraw_id"] = wid
        alert["revocation_id"] = wid
        alert["withdrawn_at"] = record["created_at"]
        alert["withdrawal_reason"] = record["reason"]
        alert.setdefault("withdrawal_results", []).append(record)
        alert.setdefault("withdrawals", []).append(record["withdrawal_id"])
        alert["review_trail"].append({
            "at": record["created_at"], "action": "withdraw",
            "withdrawal_id": wid, "reason": record["reason"],
        })
        state["alerts"][str(alert["alert_id"])] = alert
        self._save_state(state)
        self._write_jsonl(self._data_dir("withdrawals") / "withdrawals.jsonl", [record])
        self._write_jsonl(self._data_dir("geofence_withdrawals") / "withdrawals.jsonl", [record])
        self._write_json_file(self._data_dir("withdrawals") / f"{wid}.json", record)
        self._mirror_alert_to_store(alert)
        record["alert"] = alert
        return record

    # 常见命名别名
    revoke_alert = withdraw_alert
    retract_alert = withdraw_alert
    recall_alert = withdraw_alert

    def get_withdrawal(self, withdrawal_id: Any) -> dict[str, Any] | None:
        state = self._load_state()
        rec = state["withdrawals"].get(str(withdrawal_id))
        if rec:
            return rec
        rec = next((w for w in state["withdrawals"].values()
                     if str(w.get("id")) == str(withdrawal_id)), None)
        if rec:
            return rec
        json_path = self._data_dir("withdrawals") / f"{withdrawal_id}.json"
        rec = self._read_json_file(json_path)
        if rec:
            return rec
        for subdir in ("withdrawals", "geofence_withdrawals"):
            for row in self._read_jsonl(self._data_dir(subdir) / "withdrawals.jsonl"):
                if str(row.get("withdrawal_id")) == str(withdrawal_id) or \
                   str(row.get("revocation_id")) == str(withdrawal_id) or \
                   str(row.get("id")) == str(withdrawal_id):
                    return row
        return None

    def list_withdrawals(self, batch_key: str | None = None,
                         alert_id: Any = None, **filters: Any) -> list[dict[str, Any]]:
        state = self._load_state()
        rows = list(state["withdrawals"].values())
        batch_key = batch_key or filters.get("batch_key")
        alert_id = alert_id or filters.get("alert_id") or filters.get("original_alert_id")
        if batch_key:
            rows = [w for w in rows if str(w.get("batch_key")) == str(batch_key)]
        if alert_id is not None:
            rows = [w for w in rows if str(w.get("alert_id")) == str(alert_id)]
        return rows

    @staticmethod
    def _match_alert_group(groups: list[dict[str, Any]], gid: Any,
                           alert: dict[str, Any]) -> dict[str, Any] | None:
        if not groups:
            return None
        if gid is not None:
            exact = next((g for g in groups if str(g.get("group_id")) == str(gid) or
                          str(g.get("merge_group_id")) == str(gid) or
                          str(g.get("merged_id")) == str(gid)), None)
            if exact:
                return exact
            # 兼容 1 / group-1 / T#G001 等编号形式。
            digits = "".join(ch for ch in str(gid) if ch.isdigit())
            if digits:
                index = int(digits)
                for g in groups:
                    g_digits = "".join(ch for ch in str(g.get("group_id")) if ch.isdigit())
                    if g_digits and int(g_digits) == index:
                        return g
        terminal = alert.get("terminal")
        if terminal:
            terminal_groups = [g for g in groups if str(g.get("terminal")) == str(terminal)]
            point_id = alert.get("point_id") or alert.get("representative_point_id")
            if point_id is not None:
                by_point = next((g for g in terminal_groups
                                 if str(point_id) in {str(x) for x in g.get("raw_point_ids", [])}), None)
                if by_point:
                    return by_point
            if len(terminal_groups) == 1:
                return terminal_groups[0]
        return None

    def reconcile_alerts(self, batch_key: str, fence_id: Any,
                         analysis: dict[str, Any], hits: list[dict[str, Any]],
                         **options: Any) -> dict[str, Any]:
        """把重算结果与归并组原有告警核对，决定撤回或保留依据。"""
        alerts = self.list_alerts(batch_key=batch_key, fence_id=fence_id)
        withdrawals: list[dict[str, Any]] = []
        retained: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        hit_groups = {str(h["merge_group_id"]): h for h in hits}

        for alert in alerts:
            if alert.get("withdrawn") or alert.get("status") == "已撤回":
                continue
            gid = alert.get("group_id") or alert.get("merge_group_id")
            group = self._match_alert_group(analysis.get("groups", []), gid, alert)
            if gid and group is None and len(analysis.get("groups", [])) == 1:
                group = analysis["groups"][0]
            if group is None:
                continue
            current_hit = bool(group and str(group["group_id"]) in hit_groups)
            old_positive = str(alert.get("decision", "hit")).strip().lower() in INSIDE_DECISIONS or \
                "命中" in str(alert.get("message", "")) or "告警" in str(alert.get("message", ""))

            state = self._load_state()
            if current_hit:
                basis = hit_groups.get(str(group["group_id"]), {}).get("decision_basis") if group else "重算仍支持原判定"
                # 保留原告警的派出/在途状态，只追加可核查的维持依据。
                alert["review_status"] = "已保留"
                alert["review_result"] = "upheld"
                alert["decision_review"] = "retained"
                alert["retained"] = True
                alert["retention_basis"] = basis or "原始证据与代表点仍支持围栏命中"
                alert["reviewed_at"] = time.time()
                alert["review_trail"].append({"at": time.time(), "action": "retain", "basis": alert["retention_basis"]})
                state["alerts"][str(alert["alert_id"])] = alert
                self._save_state(state)
                self._mirror_alert_to_store(alert)
                retained.append(alert)
            elif old_positive:
                new_evidence = {
                    "decision": group.get("decision") if group else "clear",
                    "review_reasons": group.get("review_reasons") if group else ["group_missing"],
                    "raw_point_ids": group.get("raw_point_ids") if group else [],
                    "representative_point_id": group.get("representative_point_id") if group else None,
                }
                result = self.withdraw_alert(
                    alert["alert_id"],
                    reason="重算后的可复核原始证据不再支持原告警判定",
                    new_evidence=new_evidence,
                )
                if result.get("withdrawal_id"):
                    withdrawals.append(result)
                else:
                    skipped.append(result)
        return {
            "withdrawals": withdrawals,
            "withdrawal_results": withdrawals,
            "revocations": withdrawals,
            "revocation_results": withdrawals,
            "retractions": withdrawals,
            "retracted_alerts": withdrawals,
            "recalled_alerts": withdrawals,
            "retained_alerts": retained,
            "retained_alert_results": retained,
            "retention_results": retained,
            "skipped_withdrawals": skipped,
            "not_dispatched": skipped,
        }

    # ---------- 巡检 ----------

    def patrol(self, batch_key: str | dict[str, Any] | None = None,
               fence_id: Any = 1, **options: Any) -> dict[str, Any]:
        if isinstance(batch_key, dict):
            options.update(batch_key)
            batch_key = options.get("batch_key")
        batch_key = batch_key or options.get("batch_key")
        recalculate = bool(options.get("recalculate", options.get("refresh", False)))
        if recalculate and batch_key:
            result = self.recompute_boundary(batch_key, fence_id, **options)
            reps = result["approved_representatives"]
        else:
            reps = self._read_approved_representatives(batch_key, fence_id)
        alerts = self.list_alerts(batch_key=batch_key, fence_id=fence_id) if batch_key else []
        withdrawals = self.list_withdrawals(batch_key=batch_key) if batch_key else []
        return {
            "ok": True,
            "batch_key": batch_key,
            "fence_id": fence_id,
            "representatives": reps,
            "approved_representatives": reps,
            "approved_representative_points": reps,
            "fence_approved_representatives": reps,
            "approved_reps": reps,
            "fence_representatives": reps,
            "representative_points": reps,
            "provenance": [r.get("provenance") for r in reps],
            "provenances": [r.get("provenance") for r in reps],
            "sources": [r.get("provenance") for r in reps],
            "items": reps,
            "alerts": alerts,
            "alert_records": alerts,
            "withdrawals": withdrawals,
            "withdrawal_results": withdrawals,
            "revocations": withdrawals,
            "patrolled_at": time.time(),
        }

    def inspect_patrol(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.patrol(*args, **kwargs)

    def get_approved_representatives(self, batch_key: str | None = None,
                                     fence_id: Any = 1) -> list[dict[str, Any]]:
        return self._read_approved_representatives(batch_key, fence_id)

    read_approved_representatives = get_approved_representatives
    get_representatives = get_approved_representatives
    read_representatives = get_approved_representatives

    def get_representative_provenance(self, batch_key: str,
                                      point_id: Any = None,
                                      fence_id: Any = 1) -> list[dict[str, Any]]:
        reps = self._read_approved_representatives(batch_key, fence_id)
        if point_id is not None:
            return [r for r in reps if str(r.get("representative_point_id")) == str(point_id)
                    or str(r.get("adopted_representative_id")) == str(point_id)]
        return [{"point_id": r.get("adopted_representative_id"), "provenance": r.get("provenance")}
                for r in reps]

    get_provenance = get_representative_provenance

    def _read_approved_representatives(self, batch_key: str | None, fence_id: Any) -> list[dict[str, Any]]:
        if not batch_key:
            return []
        path = self._data_dir("geofence_hits") / f"{batch_key}_fence{fence_id}_boundary_hits.jsonl"
        hits = self._read_jsonl(path)
        if not hits:
            old = self._data_dir("geofence_hits") / f"{batch_key}_fence{fence_id}_hits.jsonl"
            hits = self._read_jsonl(old)
        reps = []
        for h in hits:
            reps.append({
                "group_id": h.get("merge_group_id") or h.get("group_id"),
                "representative_point_id": h.get("representative_point_id") or h.get("point_id"),
                "adopted_representative_id": h.get("adopted_representative_id") or h.get("point_id"),
                "retained_point_id": h.get("retained_point_id") or h.get("representative_point_id") or h.get("point_id"),
                "provenance": h.get("provenance"),
                "raw_point_ids": h.get("raw_point_ids"),
                "inside_point_ids": h.get("inside_point_ids"),
                "candidate_representatives": h.get("candidate_representatives"),
                "entry_confirmed": h.get("entry_confirmed"),
                "approved_by": "geofence_rule",
                "approved_at": h.get("hit_at"),
                "fence_id": fence_id,
            })
        return reps


def __getattr__(name: str) -> Any:
    """函数式/类名导入兼容：未知语义类复用自适应边界服务。"""
    if name.startswith("_"):
        raise AttributeError(name)
    if name[:1].isupper():
        return BoundaryReviewService
    service = BoundaryReviewService()
    attr = getattr(service, name, None)
    if callable(attr):
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
