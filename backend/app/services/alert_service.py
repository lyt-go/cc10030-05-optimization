"""告警服务命名兼容入口。"""
from app.services.geofence_alert import GeofenceAlertService


class AlertService(GeofenceAlertService):
    MODULE = "geofence_alert"
    ENTITY = "围栏告警"


class AlertManager(GeofenceAlertService):
    MODULE = "geofence_alert"
    ENTITY = "围栏告警"
