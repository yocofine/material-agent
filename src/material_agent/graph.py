"""LangGraph 状态机装配 + 运行/恢复封装。

图结构：
    START → ingest → classify → rename_upload → verify → END
               │                          │            │
               └──── 阻塞型异常 ─────► exception ◄──────┘
                                        END(挂起)

human-in-the-loop：verify 节点内 interrupt 挂起，等学员确认后 resume。
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from .constants import ExceptionType
from .models import ExceptionRecord, FileItem, OrderContext, OrderStatus
from .nodes import (
    classify_node,
    exception_node,
    ingest_node,
    rename_upload_node,
    verify_node,
)
from .state import AgentState
from .store import checkpoint_db_path, load_order, save_order

logger = logging.getLogger(__name__)

BLOCKING_TYPES = {
    ExceptionType.CORRUPT_OR_ENCRYPTED.value,
    ExceptionType.MISSING_REQUIRED.value,
    ExceptionType.UPLOAD_FAILED.value,
    ExceptionType.CONFIRMATION_REJECTED.value,
}


# --------------------------------------------------------------------------- #
# 条件边路由
# --------------------------------------------------------------------------- #
def _has_type(state: AgentState, t: str) -> bool:
    return any(e.type == t for e in state.get("exceptions", []))


def route_after_ingest(state: AgentState) -> str:
    if _has_type(state, ExceptionType.CORRUPT_OR_ENCRYPTED.value) or _has_type(
        state, ExceptionType.MISSING_REQUIRED.value
    ):
        return "exception"
    return "classify"


def route_after_classify(state: AgentState) -> str:  # noqa: ARG001
    return "rename_upload"


def route_after_rename_upload(state: AgentState) -> str:
    if _has_type(state, ExceptionType.UPLOAD_FAILED.value):
        return "exception"
    return "verify"


def route_after_verify(state: AgentState) -> str:
    if _has_type(state, ExceptionType.MISSING_REQUIRED.value) or _has_type(
        state, ExceptionType.CONFIRMATION_REJECTED.value
    ):
        return "exception"
    return END


# --------------------------------------------------------------------------- #
# 图构建
# --------------------------------------------------------------------------- #
def build_graph(checkpointer=None):
    g = StateGraph(AgentState)

    g.add_node("ingest", ingest_node)
    g.add_node("classify", classify_node)
    g.add_node("rename_upload", rename_upload_node)
    g.add_node("verify", verify_node)
    g.add_node("exception", exception_node)

    g.add_edge(START, "ingest")
    g.add_conditional_edges(
        "ingest",
        route_after_ingest,
        {"classify": "classify", "exception": "exception"},
    )
    g.add_edge("classify", "rename_upload")
    g.add_conditional_edges(
        "rename_upload",
        route_after_rename_upload,
        {"verify": "verify", "exception": "exception"},
    )
    g.add_conditional_edges("verify", route_after_verify, {"exception": "exception", END: END})
    g.add_edge("exception", END)

    return g.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())


# --------------------------------------------------------------------------- #
# 运行 / 恢复封装（单进程；跨进程 resume 见 README「切换持久化 checkpoint」）
# --------------------------------------------------------------------------- #
_graph_cache: dict[str, Any] = {}
_persistent_checkpointer: Any | None = None


def _get_persistent_checkpointer() -> Any:
    """Use a durable checkpoint store so CLI resume works in a new process."""
    global _persistent_checkpointer
    if _persistent_checkpointer is not None:
        return _persistent_checkpointer
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "缺少 langgraph-checkpoint-sqlite，请重新安装 requirements.txt"
        ) from e
    import sqlite3

    conn = sqlite3.connect(str(checkpoint_db_path()), check_same_thread=False)
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=[OrderStatus, OrderContext, FileItem, ExceptionRecord]
    )
    _persistent_checkpointer = SqliteSaver(conn, serde=serde)
    _persistent_checkpointer.setup()
    return _persistent_checkpointer


def _thread_config(order_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": order_id}}


def run_order(order: OrderContext) -> dict[str, Any]:
    """跑一条订单，遇到人工确认会挂起并返回当前状态。"""
    checkpointer = _get_persistent_checkpointer()
    # run_order starts a fresh execution. A previous attempt may have left a
    # terminal or interrupted checkpoint under the same order ID; reusing it
    # would merge old exceptions/files into the new input. resume_order is the
    # explicit API for continuing an interrupted execution.
    checkpointer.delete_thread(order.order_id)
    graph = build_graph(checkpointer=checkpointer)
    _graph_cache[order.order_id] = graph
    config = _thread_config(order.order_id)
    initial: AgentState = {
        "order": order,
        "files": [],
        "exceptions": [],
        "messages": [],
        "current_stage": "start",
    }
    result = graph.invoke(initial, config)
    _persist_result(result, order)
    return result


def resume_order(order_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    """学员确认后恢复：decision 形如 {"approved": True}。"""
    if load_order(order_id) is None:
        raise ValueError(f"订单不存在：{order_id}")
    graph = build_graph(checkpointer=_get_persistent_checkpointer())
    result = graph.invoke(Command(resume=decision), _thread_config(order_id))
    _persist_result(result)
    return result


def is_interrupted(result: dict[str, Any], order: OrderContext) -> bool:
    """判断是否停在人工确认点（等待 resume）。"""
    return order.status in (OrderStatus.AWAITING_CONFIRM, OrderStatus.EXCEPTION)


def _persist(order: OrderContext | None) -> None:
    if order is not None:
        save_order(order)


def _persist_result(result: dict[str, Any], fallback_order: OrderContext | None = None) -> None:
    """Persist the complete graph state, including files and exceptions."""
    order = result.get("order") or fallback_order
    if order is None:
        return
    order.files = list(result.get("files", []))
    order.exceptions = list(result.get("exceptions", []))
    _persist(order)
