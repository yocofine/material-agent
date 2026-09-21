"""② 文件解读与分类：规则 + LLM 语义分类。"""
from __future__ import annotations

import re

from ..classifier import classify_all
from ..constants import Category, ExceptionType
from ..llm import QwenClient
from ..models import ExceptionRecord, OrderContext, OrderStatus
from ..state import AgentState


def classify_node(state: AgentState) -> dict:
    order: OrderContext = state["order"]
    files = list(state.get("files", []))

    llm = QwenClient()
    classified = classify_all(files, llm)

    # Multiple differently-versioned copies of the same core document are
    # ambiguous. Keep the latest classification visible, but route the older
    # copies to Additional and record a non-blocking conflict for review.
    conflicts: list[ExceptionRecord] = []
    for category in (Category.REQUIREMENT.value, Category.UNIT_GUIDE.value):
        candidates = [f for f in classified if f.category == category]
        versioned = [
            f for f in candidates
            if re.search(r"(?:19|20)\d{2}|(?:version|ver|v)[ _.-]?\d+|旧|新版|旧版", f.original_name, re.I)
        ]
        if len(versioned) > 1:
            for item in versioned[:-1]:
                item.category = Category.ADDITIONAL.value
                item.note = "检测到同类不同版本，已归入 Additional"
            conflicts.append(
                ExceptionRecord(
                    ExceptionType.VERSION_CONFLICT.value,
                    f"{category} 存在多个带版本标记的文件，旧版本已归入 Additional",
                )
            )

    order.status = OrderStatus.CLASSIFIED
    return {
        "order": order,
        "files": classified,
        "exceptions": conflicts,
        "current_stage": "classify",
    }
