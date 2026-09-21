"""MCP Server 封装：把「资料归集」流程暴露为 Agent 可调用的 tools。

依赖 fastmcp。启动：
    python -m material_agent.cli serve-mcp
或直接：
    python -m material_agent.mcp_server
"""
from __future__ import annotations

import logging
from pathlib import Path

from .classifier import classify_by_rules
from .constants import CATEGORY_RULES, VERIFY_CHECKLIST
from .graph import resume_order, run_order
from .llm import QwenClient
from .models import FileItem, OrderContext
from .store import load_order, list_orders

logger = logging.getLogger(__name__)


def _build_mcp():
    try:
        from fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "缺少 fastmcp 依赖，请先 `pip install fastmcp`（或 `pip install -r requirements.txt`）"
        ) from e

    mcp = FastMCP("material-agent")

    @mcp.tool()
    def list_categories() -> list[dict]:
        """列出所有材料类别、优先级与识别关键词。"""
        return [
            {
                "category": r.category.value,
                "priority": r.priority.value,
                "zh_name": r.zh_name,
                "keywords": list(r.keywords),
                "description": r.description,
            }
            for r in CATEGORY_RULES
        ]

    @mcp.tool()
    def classify_document(path: str) -> dict:
        """对单个文件做规则分类（不调 LLM），返回命中的类别或 None。"""
        p = Path(path)
        hit = classify_by_rules(p.name, "")
        return {
            "path": path,
            "category": hit[0].value if hit else None,
            "matched_keyword": hit[1] if hit else "",
        }

    @mcp.tool()
    def run_order(order_id: str, source: str, course_code: str = "",
                  assignment_type: str = "", student_id: str = "") -> dict:
        """启动一条订单的资料归集全流程（会停在人工确认点）。"""
        order = OrderContext(
            order_id=order_id,
            course_code=course_code,
            assignment_type=assignment_type,
            student_id=student_id,
            source=source,
        )
        result = run_order(order)
        return {
            "order_id": order_id,
            "status": result["order"].status.value,
            "files": [f.to_dict() for f in result.get("files", [])],
            "exceptions": [e.to_dict() for e in result.get("exceptions", [])],
        }

    @mcp.tool()
    def get_order_status(order_id: str) -> dict | None:
        """查询订单当前状态与文件清单。"""
        order = load_order(order_id)
        if order is None:
            return None
        return {"status": order.status.value, "order": order.to_dict()}

    @mcp.tool()
    def confirm_order(order_id: str, approved: bool) -> dict:
        """学员二次确认后恢复流程（approved=True 通过 / False 退回）。"""
        result = resume_order(order_id, {"approved": approved})
        return {
            "order_id": order_id,
            "status": result.get("order").status.value if result.get("order") else "unknown",
        }

    @mcp.tool()
    def get_verify_checklist() -> list[str]:
        """返回二次核对的核对清单模板。"""
        return list(VERIFY_CHECKLIST)

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _build_mcp().run()


if __name__ == "__main__":
    main()
