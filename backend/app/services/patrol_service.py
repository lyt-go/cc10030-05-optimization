"""巡检服务命名兼容入口。"""
from app.services.boundary_review import BoundaryReviewService


class PatrolService(BoundaryReviewService):
    MODULE = "boundary_review"
    ENTITY = "围栏巡检"
