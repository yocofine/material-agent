"""① 接收与预处理：下载 → 统一存放【订单编号】目录 → 解压 → 完整性校验 → 建索引。"""
from __future__ import annotations

import logging
import ipaddress
import shutil
import socket
import zipfile
import time
from urllib.parse import urlsplit
from uuid import uuid4
from pathlib import Path

import httpx

from ..config import get_settings
from ..constants import ExceptionType
from ..archive_utils import extract_rar_safe, extract_tar_safe, extract_zip_recursive, safe_filename
from ..models import ExceptionRecord, FileItem, OrderContext, OrderStatus
from ..state import AgentState

logger = logging.getLogger(__name__)

SUPPORTED_ARCHIVE = {".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".rar"}


def ingest_node(state: AgentState) -> dict:
    order: OrderContext = state["order"]
    settings = get_settings()

    # 工作目录：workspace/<order_id>
    workspace_root = settings.workspace_path.resolve()
    if order.work_dir:
        work_dir = Path(order.work_dir).resolve()
        if work_dir != workspace_root and workspace_root not in work_dir.parents:
            raise ValueError("订单工作目录必须位于 workspace 内")
    else:
        if not _is_safe_component(order.order_id):
            raise ValueError("订单号包含非法路径字符")
        work_dir = (workspace_root / order.order_id).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    order.work_dir = str(work_dir)
    run_dir = work_dir / "runs" / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    files_dir = run_dir / "files"
    run_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)

    files: list[FileItem] = []
    exceptions: list[ExceptionRecord] = []

    src = order.source
    if not src:
        exceptions.append(ExceptionRecord(ExceptionType.MISSING_REQUIRED.value, "未提供资料来源（压缩包/目录/链接）"))
        order.status = OrderStatus.EXCEPTION
        return {"order": order, "files": files, "exceptions": exceptions, "current_stage": "ingest"}

    # 1) 下载（网盘/直链）
    local_src = _maybe_download(src, run_dir)

    # 2) 解压或枚举
    try:
        _materialize(local_src, files_dir)
    except Exception as e:  # noqa: BLE001
        exceptions.append(ExceptionRecord(ExceptionType.CORRUPT_OR_ENCRYPTED.value, f"解压失败：{e}", Path(src).name))

    # 3) 枚举文件 + 完整性校验（加密检测）
    for p in sorted(files_dir.rglob("*")):
        if not p.is_file():
            continue
        if _is_encrypted_zip(p):
            exceptions.append(
                ExceptionRecord(ExceptionType.CORRUPT_OR_ENCRYPTED.value, "压缩包已加密，需学生提供密码", p.name)
            )
            continue
        files.append(
            FileItem(
                order_id=order.order_id,
                original_name=p.name,
                local_path=str(p),
            )
        )

    if not files and not exceptions:
        exceptions.append(ExceptionRecord(ExceptionType.MISSING_REQUIRED.value, "未找到任何文件"))

    order.status = OrderStatus.INGESTED
    return {
        "order": order,
        "files": files,
        "exceptions": exceptions,
        "current_stage": "ingest",
    }


def _maybe_download(src: str, work_dir: Path) -> str:
    """若 source 是 URL，下载到工作目录；否则原样返回本地路径。"""
    if not src.startswith(("http://", "https://")):
        return src
    _reject_private_url(src)
    name = src.split("?")[0].split("/")[-1] or "download.bin"
    target = work_dir / "source" / safe_filename(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    logger.info("下载 %s -> %s", src, target)
    # Do not follow redirects after validating the original URL.
    with httpx.stream("GET", src, follow_redirects=False, timeout=120) as r:
        r.raise_for_status()
        with open(target, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return str(target)


def _is_safe_component(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char in "._-" for char in value) and len(value) <= 128


def _reject_private_url(url: str) -> None:
    host = urlsplit(url).hostname
    if not host:
        raise ValueError("下载地址缺少主机名")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror as e:
        raise ValueError(f"无法解析下载地址：{host}") from e
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("禁止下载内网或保留地址")


def _materialize(src: str, work_dir: Path) -> None:
    """解压归档到工作目录；目录则复制内容。"""
    p = Path(src)
    if p.is_dir():
        for child in p.iterdir():
            dest = work_dir / child.name
            if child.is_dir():
                shutil.copytree(child, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(child, dest)
        return

    suffix = p.suffix.lower()
    if suffix in {".zip"}:
        extract_zip_recursive(p, work_dir)
    elif suffix in {".tar", ".gz", ".tgz"}:
        extract_tar_safe(p, work_dir)
    elif suffix in {".rar"}:
        # 可选依赖 rarfile + unrar 可执行文件
        extract_rar_safe(p, work_dir)
    else:
        # 单个文件：复制进工作目录
        shutil.copy2(p, work_dir / p.name)


def _is_encrypted_zip(path: Path) -> bool:
    if path.suffix.lower() != ".zip":
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return any(fi.flag_bits & 0x1 for fi in zf.infolist())
    except (zipfile.BadZipFile, OSError):
        return True  # 打不开视为损坏
