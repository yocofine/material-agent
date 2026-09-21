"""④ 二次核对与反馈：核对清单 → 发学员确认（human-in-the-loop）→ 标记完成。"""
from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from ..constants import REQUIRED_CATEGORIES, VERIFY_CHECKLIST, Category, ExceptionType
from ..models import ExceptionRecord, FileItem, OrderContext, OrderStatus
from ..order_api import OrderApiClient
from ..state import AgentState

def verify_node(state: AgentState) -> dict:
    order: OrderContext = state["order"]
    files: list[FileItem] = list(state.get("files", []))
    exceptions: list[ExceptionRecord] = []

    # 1) 必传项完整性检查
    present = {f.category for f in files}
    configured_required = OrderApiClient().get_required_categories(order.order_id)
    required_categories = (
        configured_required
        if configured_required is not None
        else [c.value for c in REQUIRED_CATEGORIES]
    )
    missing_required = [category for category in required_categories if category not in present]

    # 2) 需要学员确认的「模糊分类」文件
    needs_confirm = [f.original_name for f in files if f.needs_confirmation]

    # 3) 组装核对清单
    checklist_lines = list(VERIFY_CHECKLIST)
    if missing_required:
        checklist_lines.append("⚠️ 当前缺少必传项：" + "、".join(missing_required))
    if needs_confirm:
        checklist_lines.append("❓ 以下文件分类不确定，请确认：" + "、".join(needs_confirm))

    # 4) human-in-the-loop：有缺项或模糊项 → 挂起等学员确认
    if missing_required or needs_confirm:
        # 挂起前先标记「等待确认」，保证 interrupt 停住时状态正确
        order.status = OrderStatus.AWAITING_CONFIRM
        decision: Any = interrupt(
            {
                "type": "human_confirm",
                "order_id": order.order_id,
                "missing_required": missing_required,
                "needs_confirmation": needs_confirm,
                "checklist": checklist_lines,
            }
        )
        approved = bool(decision.get("approved", False)) if isinstance(decision, dict) else False
        order.confirm_approved = approved

        if missing_required:
            exceptions.append(
                ExceptionRecord(
                    ExceptionType.MISSING_REQUIRED.value,
                    "缺少必传项：" + "、".join(missing_required) + "，需学员补传",
                )
            )
        elif not order.confirm_approved:
            exceptions.append(
                ExceptionRecord(
                    ExceptionType.CONFIRMATION_REJECTED.value,
                    "学员未确认核对清单，需要人工指出需调整项",
                )
            )
    else:
        # 全部齐全且无模糊项 → 自动通过
        order.confirm_approved = True

    # 5) 状态推进
    if order.confirm_approved and not missing_required:
        order.status = OrderStatus.COMPLETED
    else:
        order.status = OrderStatus.AWAITING_CONFIRM

    return {
        "order": order,
        "files": files,
        "exceptions": exceptions,
        "current_stage": "verify",
    }
