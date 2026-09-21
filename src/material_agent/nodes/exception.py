"""⑤ 异常处理：汇总异常 → 通知 → 阻塞型挂起 / 非阻塞型继续。"""
from __future__ import annotations

import logging

from ..constants import EXCEPTION_HANDLERS, ExceptionType
from ..models import ExceptionRecord, OrderContext, OrderStatus
from ..state import AgentState

logger = logging.getLogger(__name__)

# 阻塞型：必须等学生重发/补传，流程暂停
BLOCKING = {
    ExceptionType.CORRUPT_OR_ENCRYPTED.value,
    ExceptionType.MISSING_REQUIRED.value,
    ExceptionType.UPLOAD_FAILED.value,
    ExceptionType.CONFIRMATION_REJECTED.value,
}


def exception_node(state: AgentState) -> dict:
    order: OrderContext = state["order"]
    exceptions: list[ExceptionRecord] = list(state.get("exceptions", []))

    lines = []
    for e in exceptions:
        try:
            handler = EXCEPTION_HANDLERS.get(ExceptionType(e.type), "人工介入处理")
        except ValueError:
            handler = "人工介入处理"
        lines.append(f"- 【{e.type}】{e.message} → {handler}")

    if lines:
        logger.warning("订单 %s 异常：\n%s", order.order_id, "\n".join(lines))

    has_blocking = any(e.type in BLOCKING for e in exceptions)
    if has_blocking:
        order.status = OrderStatus.EXCEPTION
    else:
        # 非阻塞异常（版本冲突等）已在前面处理，继续流程
        order.status = OrderStatus.COMPLETED

    return {
        "order": order,
        # The existing records are already in state; returning them here would
        # append the same errors a second time via the reducer.
        "exceptions": [],
        "current_stage": "exception",
    }
