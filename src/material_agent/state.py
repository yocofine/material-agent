"""LangGraph 状态定义。

采用「整体替换」的简单约定：每个节点返回需要更新的字段，图内部自动 merge。
files / exceptions / messages 使用 Annotated + 自定义 reducer 控制累积行为。
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages

from .models import ExceptionRecord, FileItem, OrderContext


def _replace_files(old: list[FileItem], new: list[FileItem]) -> list[FileItem]:
    """files 整体替换：节点返回新的完整列表。"""
    return new


def _append_exceptions(
    old: list[ExceptionRecord], new: list[ExceptionRecord]
) -> list[ExceptionRecord]:
    return old + new


class AgentState(TypedDict, total=False):
    order: OrderContext
    files: Annotated[list[FileItem], _replace_files]
    exceptions: Annotated[list[ExceptionRecord], _append_exceptions]
    messages: Annotated[list[Any], add_messages]  # 保留给 LLM 节点用
    current_stage: str
    # human-in-the-loop：verify 节点通过 interrupt 挂起后，恢复时写入此字段
    human_feedback: dict[str, Any]
