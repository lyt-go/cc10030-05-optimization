"""撤回结果服务命名兼容入口。"""
from app.services.boundary_review import BoundaryReviewService


class WithdrawalService(BoundaryReviewService):
    MODULE = "boundary_review"
    ENTITY = "告警撤回"


class RevocationService(BoundaryReviewService):
    MODULE = "boundary_review"
    ENTITY = "告警撤回"
