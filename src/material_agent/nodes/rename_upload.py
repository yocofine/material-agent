"""③ 标准化命名与上传：按模板重命名 → 调订单系统上传 → 打勾标记。"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..constants import NAMING_TEMPLATE, safe_category_token, Category, ExceptionType
from ..models import ExceptionRecord, FileItem, OrderContext, OrderStatus
from ..order_api import OrderApiClient
from ..state import AgentState

logger = logging.getLogger(__name__)


def build_target_name(item: FileItem, order: OrderContext) -> str:
    """按命名模板生成目标文件名：Requirement_ACCT1001_Essay.pdf"""
    cat = item.category or Category.OTHER.value
    try:
        token = safe_category_token(Category(cat))
    except ValueError:
        token = safe_category_token(Category.OTHER)
    course = _safe_token(order.course_code or "NONE")
    atype = _safe_token(order.assignment_type or "General")
    ext = Path(item.local_path).suffix if item.local_path else ".pdf"
    return f"{NAMING_TEMPLATE.format(category=token, course_code=course, assignment_type=atype)}{ext}"


def _safe_token(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value or "General"


def rename_upload_node(state: AgentState) -> dict:
    order: OrderContext = state["order"]
    files = list(state.get("files", []))
    exceptions: list[ExceptionRecord] = []

    client = OrderApiClient()
    used_names: set[str] = set()
    for item in files:
        if not item.local_path or item.uploaded:
            continue
        # 1) 重命名（本地复制为规范名，保留原文件；同名自动加序号）
        item.target_name = _dedupe(build_target_name(item, order), used_names)
        target_path = Path(item.local_path).parent / "__normalized__" / item.target_name
        try:
            if Path(item.local_path) != target_path:
                _copy_as(item.local_path, target_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("重命名失败 %s：%s", item.local_path, e)

        # 2) 上传到订单系统附件区
        try:
            result = client.upload_attachment(
                order_id=order.order_id,
                student_id=order.student_id,
                file_path=str(target_path),
                target_name=item.target_name,
                category=item.category or Category.OTHER.value,
            )
            item.uploaded = bool(result.get("ok", True))
            if not item.uploaded:
                exceptions.append(
                    ExceptionRecord(ExceptionType.UPLOAD_FAILED.value, f"上传被拒：{item.target_name}")
                )
        except Exception as e:  # noqa: BLE001
            item.uploaded = False
            exceptions.append(
                ExceptionRecord(ExceptionType.UPLOAD_FAILED.value, f"{item.target_name} 上传失败：{e}")
            )

    order.status = OrderStatus.UPLOADED
    return {
        "order": order,
        "files": files,
        "exceptions": exceptions,
        "current_stage": "rename_upload",
    }


def _copy_as(src: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = Path(src).read_bytes()
    dst.write_bytes(data)


def _dedupe(name: str, used: set[str]) -> str:
    """同名文件加 _2/_3 后缀，避免覆盖。"""
    if name not in used:
        used.add(name)
        return name
    p = Path(name)
    i = 2
    while True:
        candidate = f"{p.stem}_{i}{p.suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1
