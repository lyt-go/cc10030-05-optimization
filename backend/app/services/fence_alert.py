"""围栏告警模块名兼容入口。"""
from app.services.geofence_alert import GeofenceAlertService


class AlertService(GeofenceAlertService):
    MODULE = "geofence_alert"
    ENTITY = "围栏告警"


FenceAlertService = GeofenceAlertService

__all__ = ["GeofenceAlertService", "FenceAlertService", "AlertService"]
