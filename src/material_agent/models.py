"""核心数据模型（纯 dataclass，便于序列化与持久化）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class OrderStatus(str, Enum):
    PENDING = "pending"               # 待处理（已建订单，未开始归集）
    INGESTED = "ingested"             # ① 已接收/解压/建索引
    CLASSIFIED = "classified"         # ② 已分类
    UPLOADED = "uploaded"             # ③ 已命名并上传
    AWAITING_CONFIRM = "awaiting_confirm"  # ④ 已发核对清单，等学员确认
    COMPLETED = "completed"           # 归集完成
    EXCEPTION = "exception"           # ⑤ 异常挂起


@dataclass
class FileItem:
    order_id: str
    original_name: str
    local_path: str = ""
    path_context: str = ""
    category: str | None = None            # Category.value
    confidence: float = 0.0
    classified_by: str = ""                # rule | llm | manual
    matched_keyword: str = ""
    extracted_text: str = ""               # 前 N 字符，仅用于分类，不落库全文
    target_name: str = ""
    uploaded: bool = False
    needs_confirmation: bool = False
    timed_out: bool = False
    note: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExceptionRecord:
    type: str                              # ExceptionType.value
    message: str
    file_name: str = ""
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderContext:
    order_id: str
    course_code: str = ""
    assignment_type: str = ""
    student_id: str = ""
    student_name: str = ""
    status: OrderStatus = OrderStatus.PENDING
    source: str = ""                       # 压缩包路径 / 网盘链接 / 目录
    work_dir: str = ""                     # 本订单的本地工作目录
    files: list[FileItem] = field(default_factory=list)
    exceptions: list[ExceptionRecord] = field(default_factory=list)
    confirm_approved: bool | None = None   # 学员二次确认结果

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d
