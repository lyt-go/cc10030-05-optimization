"""单数模块名兼容入口。"""
from app.services.alert_service import AlertManager, AlertService

__all__ = ["AlertService", "AlertManager"]
