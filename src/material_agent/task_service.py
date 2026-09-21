"""Minimal background execution for fileUrl classification tasks."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import shutil
import socket
import tarfile
import tempfile
import threading
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from .archive_utils import extract_rar_safe, extract_tar_safe, extract_zip_recursive, safe_filename
from .classifier import classify_all_async
from .config import get_settings
from .llm import QwenClient
from .models import FileItem
from .task_store import update_task

logger = logging.getLogger(__name__)
_worker_semaphore: threading.BoundedSemaphore | None = None
_worker_limit = 0


def redact_url(url: str) -> str:
    """Remove signed query parameters before persisting the source URL."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _suffix(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".tar.gz"):
        return ".tar.gz"
    return Path(lowered).suffix


def _semaphore() -> threading.BoundedSemaphore:
    global _worker_semaphore, _worker_limit
    limit = min(max(get_settings().task_worker_concurrency, 1), 16)
    if _worker_semaphore is None or _worker_limit != limit:
        _worker_semaphore = threading.BoundedSemaphore(limit)
        _worker_limit = limit
    return _worker_semaphore


def _validate_download_host(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("fileUrl 必须是有效的 HTTP/HTTPS 地址")
    if get_settings().task_allow_private_urls:
        return
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as exc:
        raise ValueError("fileUrl 域名无法解析") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("fileUrl 不允许访问内网或保留地址")


def _download(url: str, destination: Path, filename_hint: str = "") -> Path:
    _validate_download_host(url)
    settings = get_settings()
    parsed = urlsplit(url)
    filename = safe_filename(
        filename_hint or unquote(Path(parsed.path).name) or "download.bin"
    )
    target = destination / filename
    is_archive = _suffix(filename) in {".zip", ".tar", ".tar.gz", ".tgz", ".rar"}
    max_bytes = (
        settings.api_max_archive_mb if is_archive else settings.api_max_file_mb
    ) * 1024 * 1024
    total = 0
    with httpx.stream(
        "GET",
        url,
        follow_redirects=False,
        timeout=settings.task_download_timeout,
    ) as response:
        if response.status_code >= 400:
            raise ValueError(f"文件下载失败：HTTP {response.status_code}")
        content_length = int(response.headers.get("content-length", "0") or 0)
        if content_length > max_bytes:
            raise ValueError(f"下载文件超过 {max_bytes // (1024 * 1024)} MB 限制")
        with target.open("wb") as output:
            for chunk in response.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"下载文件超过 {max_bytes // (1024 * 1024)} MB 限制")
                output.write(chunk)
    return target


def _documents(downloaded: Path, root: Path) -> list[Path]:
    suffix = _suffix(downloaded.name)
    if suffix not in {".zip", ".tar", ".tar.gz", ".tgz", ".rar"}:
        return [downloaded]
    settings = get_settings()
    extracted = root / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    size_limit = settings.api_max_extracted_mb * 1024 * 1024
    file_limit = settings.api_max_extracted_files
    if suffix == ".zip":
        extract_zip_recursive(
            downloaded, extracted, max_total_bytes=size_limit, max_files=file_limit
        )
    elif suffix in {".tar", ".tar.gz", ".tgz"}:
        extract_tar_safe(
            downloaded, extracted, max_total_bytes=size_limit, max_files=file_limit
        )
    else:
        extract_rar_safe(
            downloaded, extracted, max_total_bytes=size_limit, max_files=file_limit
        )
    archive_suffixes = {".zip", ".tar", ".tar.gz", ".tgz", ".rar"}
    return sorted(
        path
        for path in extracted.rglob("*")
        if path.is_file() and _suffix(path.name) not in archive_suffixes
    )


def _result_summary(items: list[dict]) -> tuple[str, str]:
    if len(items) == 1:
        item = items[0]
        return item["type"], item["summary"] or f"分类为 {item['type']}"
    counts = Counter(item["type"] for item in items)
    result_type = next(iter(counts)) if len(counts) == 1 else "Multiple"
    summary = "共分类 {} 个文件：{}".format(
        len(items), "、".join(f"{category} {count} 个" for category, count in sorted(counts.items()))
    )
    return result_type, summary


def process_classification_task(task_id: str, file_url: str) -> None:
    """Download and classify one URL; designed for FastAPI BackgroundTasks."""
    with _semaphore():
        update_task(task_id, status="running")
        try:
            llm = QwenClient()
            if not llm.available:
                raise RuntimeError("QWEN_API_KEY 未配置，无法执行纯大模型分类")
            settings = get_settings()
            with tempfile.TemporaryDirectory(
                prefix=f"task-{task_id[:8]}-", dir=settings.workspace_path
            ) as temp_dir:
                root = Path(temp_dir)
                download_dir = root / "download"
                download_dir.mkdir()
                downloaded = _download(file_url, download_dir)
                documents = _documents(downloaded, root)
                if not documents:
                    raise ValueError("文件或压缩包中没有可分类内容")
                file_items = [
                    FileItem(
                        order_id=f"TASK:{task_id}",
                        original_name=path.name,
                        local_path=str(path),
                        path_context=str(path.relative_to(root)),
                    )
                    for path in documents
                ]
                classified = asyncio.run(
                    classify_all_async(
                        file_items,
                        llm,
                        use_rules=False,
                        concurrency=min(
                            max(settings.task_classification_concurrency, 1), 16
                        ),
                    )
                )
                failed = [item.original_name for item in classified if item.classified_by != "llm"]
                if failed:
                    raise RuntimeError("以下文件未能完成大模型分类：" + "、".join(failed[:5]))
                items = [
                    {
                        "filename": item.original_name,
                        "type": item.category,
                        "summary": item.note,
                        "confidence": item.confidence,
                        "promptTokens": item.prompt_tokens,
                        "completionTokens": item.completion_tokens,
                        "totalTokens": item.total_tokens,
                        "elapsedMs": item.elapsed_ms,
                    }
                    for item in classified
                ]
                result_type, summary = _result_summary(items)
                update_task(
                    task_id,
                    status="completed",
                    result_type=result_type,
                    summary=summary,
                    items=items,
                )
        except (ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
            update_task(task_id, status="failed", error=str(exc)[:1000])
        except Exception as exc:  # noqa: BLE001
            logger.exception("分类任务 %s 执行失败", task_id)
            update_task(task_id, status="failed", error="任务执行失败，请联系管理员")
