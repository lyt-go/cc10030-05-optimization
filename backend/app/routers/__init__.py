"""路由汇总：边界复核显式接口优先注册，随后注册通用资源路由。"""
from __future__ import annotations

from app.routers import boundary, resource

ROUTERS = [boundary, resource]
