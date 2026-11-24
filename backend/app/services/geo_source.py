"""定位源接入领域服务：采集定位点数据批次登记、采样频率与采集水位推进。"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GeoSourceService(BaseModuleService):
    MODULE = "geo_source"
    ENTITY = "采集批次"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["批次编号", "采集终端", "定位方式", "采样频率", "记录条数", "时间跨度", "采集水位", "批次校验值"]
    REQUIRED_FIELDS = ["批次编号", "采集终端", "定位方式"]
    STATUS_ORDER = ["草稿", "待校验", "已接入", "已拒收"]
    ACTION_RULES = {"提交校验": "待校验", "确认接入": "已接入", "标记拒收": "已拒收"}
    NEGATIVE_ACTIONS = []

    def onboard_batch(self, values: dict[str, Any], point_count: int = 50) -> dict[str, Any]:
        """接入新的定位点采集批次，生成模拟定位点序列并写入定位点数据集文件。"""
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"缺少必填字段: {missing}")
        batch_key = entry["批次编号"]
        # 写入采集位点
        self._write_checkpoint(f"geo_collect_{batch_key}", {
            "batch_key": batch_key,
            "offset": 0,
            "point_count": point_count,
            "status": "active",
            "created_at": time.time(),
        })
        # 生成模拟定位点并写入定位数据文件
        points = self._generate_sample_points(count=point_count)
        for p in points:
            p["batch_key"] = batch_key
            p["terminal"] = entry.get("采集终端", "UNKNOWN")
        output_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        written = self._write_jsonl(output_file, points)
        # 更新记录条数
        entry["记录条数"] = str(written)
        first_ts = min(p["timestamp"] for p in points)
        last_ts = max(p["timestamp"] for p in points)
        entry["时间跨度"] = f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(first_ts))} ~ {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(last_ts))}"
        checksum = self._compute_checksum(json.dumps(points, default=str))
        entry["批次校验值"] = checksum
        entry["采集水位"] = f"已采集{written}条"
        return entry

    def confirm_batch(self, batch_id: int) -> dict[str, Any]:
        """确认接入采集批次，自动触发坐标标准化和网格索引生成。"""
        entry, msg = self.run_action(batch_id, "确认接入")
        if entry is None:
            return {"error": msg, "batch_id": batch_id}
        batch_key = entry["批次编号"]

        triggers = {}
        # 自动触发坐标标准化
        try:
            coord_svc = self._safe_import("app.services.coord_transform", "CoordTransformService")()
            coord_result = coord_svc.transform_batch(batch_key)
            triggers["coord_transform"] = coord_result
        except Exception as e:
            triggers["coord_transform"] = {"error": str(e)}

        # 自动触发点位归并
        try:
            dedup_svc = self._safe_import("app.services.dedup_merge", "DedupMergeService")()
            dedup_result = dedup_svc.dedup_batch(batch_key)
            triggers["dedup_merge"] = dedup_result
        except Exception as e:
            triggers["dedup_merge"] = {"error": str(e)}

        # 自动触发网格索引生成
        try:
            grid_svc = self._safe_import("app.services.grid_index", "GridIndexService")()
            grid_result = grid_svc.index_points_from_batch(batch_key)
            triggers["grid_index"] = grid_result
        except Exception as e:
            triggers["grid_index"] = {"error": str(e)}

        return {
            "batch_entry": entry,
            "batch_key": batch_key,
            "confirmed_at": time.time(),
            "auto_triggers": triggers,
        }

    def get_batch_points(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取指定采集批次的定位点数据文件，返回定位点记录列表和文件路径。"""
        file_path = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not file_path.exists():
            raise FileNotFoundError(f"采集批次 {batch_key} 的定位点数据文件不存在")
        points = self._read_jsonl(file_path)
        return points, str(file_path)

    def save_points(self, batch_key: str, points: list[dict[str, Any]],
                    *, append: bool = False) -> dict[str, Any]:
        """写入测试或接入侧核定的原始定位点；默认覆盖当前批次证据文件。"""
        normalized = []
        for index, point in enumerate(points or [], 1):
            item = dict(point)
            item.setdefault("point_id", item.get("id", index))
            item.setdefault("batch_key", batch_key)
            normalized.append(item)
        path = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if append:
            written = self._write_jsonl(path, normalized)
            total = len(self._read_jsonl(path))
        else:
            written = self._write_jsonl_overwrite(path, normalized)
            total = written
        rows = store.rows(self.MODULE)
        entry = next((r for r in rows if r.get("批次编号") == batch_key), None)
        if entry is not None:
            entry["记录条数"] = str(total)
            entry["采集水位"] = f"已采集{total}条"
        return {"batch_key": batch_key, "written": written, "total": total, "output_file": str(path)}

    def write_points(self, batch_key: str, points: list[dict[str, Any]]) -> dict[str, Any]:
        return self.save_points(batch_key, points, append=False)

    def append_points(self, batch_key: str, points: list[dict[str, Any]]) -> dict[str, Any]:
        return self.save_points(batch_key, points, append=True)

    def upsert_point(self, batch_key: str, point: dict[str, Any]) -> dict[str, Any]:
        points, _ = self.get_batch_points(batch_key)
        pid = point.get("point_id", point.get("id"))
        replaced = False
        for index, existing in enumerate(points):
            if str(existing.get("point_id", existing.get("id"))) == str(pid):
                points[index] = {**existing, **point}
                replaced = True
                break
        if not replaced:
            points.append(point)
        return self.save_points(batch_key, points)

    def list_active_batches(self) -> list[dict[str, Any]]:
        """获取所有已接入的活跃采集批次列表。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("status") == "已接入"]