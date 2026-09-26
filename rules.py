"""返还交接业务规则（与留痕、页面入口分开维护）。

本模块只描述状态、流转、角色、交付方式与阻塞判定，不碰数据库、HTTP
和页面：需要调整返还流程时改这里即可，app.py 的留痕与 web/ 下的入口
无需变动。
"""
from __future__ import annotations

# -- 主张状态 --------------------------------------------------------------
SUBMITTED = "submitted"
UNDER_REVIEW = "under_review"
NEGOTIATING = "negotiating"
RETURN_IN_PROGRESS = "return_in_progress"   # 已建交接单，待交付
RESOLVED_RETURN = "resolved_return"
REJECTED = "rejected"

CLAIM_STATUSES = (
    SUBMITTED, UNDER_REVIEW, NEGOTIATING,
    RETURN_IN_PROGRESS, RESOLVED_RETURN, REJECTED,
)

# 审查员可手工推动的流转。进入/退出“交接中”只能由交接单动作驱动：
# negotiating --建单--> return_in_progress --签收--> resolved_return，
# return_in_progress --拒收/失效--> negotiating / under_review。
CLAIM_TRANSITIONS = {
    SUBMITTED: {UNDER_REVIEW},
    UNDER_REVIEW: {NEGOTIATING, REJECTED},
    NEGOTIATING: {REJECTED},
    RETURN_IN_PROGRESS: set(),
    RESOLVED_RETURN: set(),
    REJECTED: set(),
}

# -- 返还交接单状态 --------------------------------------------------------
PENDING_OUTBOUND = "pending_outbound"   # 已建单，待工作人员出库
OUTBOUND = "outbound"                   # 已出库，待接收人签收
SIGNED = "signed"                       # 已签收，主张置为已返还
DELIVERY_REJECTED = "rejected"          # 接收人拒收
INVALID = "invalid"                     # 待交付期间依据变化，单据失效

HANDOVER_STATUSES = (PENDING_OUTBOUND, OUTBOUND, SIGNED, DELIVERY_REJECTED, INVALID)

HANDOVER_TRANSITIONS = {
    PENDING_OUTBOUND: {OUTBOUND, DELIVERY_REJECTED, INVALID},
    OUTBOUND: {SIGNED, DELIVERY_REJECTED, INVALID},
    SIGNED: set(),
    DELIVERY_REJECTED: set(),
    INVALID: set(),
}

ACTIVE_STATUSES = {PENDING_OUTBOUND, OUTBOUND}
TERMINAL_STATUSES = {SIGNED, DELIVERY_REJECTED, INVALID}

# 只有协商阶段可以建单；拒收后回到协商；失效后退回重核。
CREATE_HANDOVER_FROM = {NEGOTIATING}
REJECT_DELIVERY_TO = NEGOTIATING
INVALIDATE_TO = UNDER_REVIEW

# -- 交付方式与角色 --------------------------------------------------------
DELIVERY_METHODS = {
    "on_site": "现场交接",
    "courier": "馆方运送",
    "third_party": "第三方物流",
}

# 各交接动作允许的系统角色。接收人若关联了系统内主张人账号，
# 则只能由本人签收/拒收；否则由工作人员代为登记。
HANDOVER_ACTION_ROLES = {
    "create": {"reviewer"},
    "outbound": {"staff"},
    "sign": {"staff", "claimant"},
    "reject": {"staff", "claimant"},
    "view": {"staff", "reviewer", "claimant"},
}


def freeze_blocks(handover, current_object_version, current_evidence_digest):
    """对比冻结依据与现状，返回阻塞原因（纯函数，便于单测与页面共用）。"""
    blocks = []
    if handover["frozen_object_version"] != current_object_version:
        blocks.append({
            "code": "object_version_changed",
            "message": (
                f"冻结的藏品版本为 v{handover['frozen_object_version']}，"
                f"当前为 v{current_object_version}，交接依据已变化"
            ),
            "frozen_object_version": handover["frozen_object_version"],
            "current_object_version": current_object_version,
        })
    if handover["evidence_digest"] != current_evidence_digest:
        blocks.append({
            "code": "evidence_changed",
            "message": "冻结后证据清单发生新增或变更，证据摘要与建单时不一致",
            "frozen_evidence_digest": handover["evidence_digest"],
            "current_evidence_digest": current_evidence_digest,
        })
    return blocks
