"""边界重算服务命名兼容入口。"""
from app.services.boundary_review import BoundaryReviewService


class BoundaryRecomputeService(BoundaryReviewService):
    MODULE = "boundary_review"
    ENTITY = "边界重算"


class BoundaryDedupService(BoundaryReviewService):
    MODULE = "dedup_merge"
    ENTITY = "边界归并"
