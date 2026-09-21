"""Background processing for the Classbro CRM classification contract."""
from __future__ import annotations

import asyncio
import logging
import tarfile
import tempfile
import threading
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .classifier import classify_all_async
from .config import get_settings
from .llm import QwenClient
from .models import FileItem
from .partner_task_store import complete_task
from .task_service import _documents, _download, _semaphore

logger = logging.getLogger(__name__)
_active_lock = threading.Lock()
_active_tasks: dict[str, str] = {}
_download_gate_lock = threading.Lock()
_download_gate: threading.BoundedSemaphore | None = None


def reserve_partner_task(task_id: str) -> bool:
    """Reserve one background slot before returning the accept response."""
    with _active_lock:
        if task_id in _active_tasks:
            return False
        _active_tasks[task_id] = "queued"
        return True


def _start_partner_task(task_id: str) -> bool:
    with _active_lock:
        state = _active_tasks.get(task_id)
        if state == "running":
            return False
        _active_tasks[task_id] = "running"
        return True


def _release_partner_task(task_id: str) -> None:
    with _active_lock:
        _active_tasks.pop(task_id, None)


def _download_concurrency() -> int:
    return min(max(get_settings().task_download_concurrency, 1), 10)


def _download_semaphore() -> threading.BoundedSemaphore:
    """Limit concurrent OSS downloads globally across all active tasks."""
    global _download_gate
    with _download_gate_lock:
        if _download_gate is None:
            _download_gate = threading.BoundedSemaphore(_download_concurrency())
        return _download_gate


def _flatten_files(payload: dict[str, Any]) -> list[dict[str, str]]:
    flattened: list[dict[str, str]] = []
    for course in payload.get("courses", []):
        for file_data in course.get("files", []):
            flattened.append(
                {
                    "courseId": str(course.get("courseId", "")),
                    "courseName": str(course.get("courseName", "")),
                    "fileId": str(file_data.get("fileId", "")),
                    "fileName": str(file_data.get("fileName", "")),
                    "fileUrl": str(file_data.get("fileUrl", "")),
                }
            )
    return flattened


def _failure(source: dict[str, str], reason: str) -> dict[str, Any]:
    return {
        "fileId": source["fileId"],
        "courseId": source["courseId"],
        "success": False,
        "failReason": reason[:1000],
    }


def _success(source: dict[str, str], items: list[FileItem]) -> dict[str, Any]:
    categories = Counter(str(item.category or "Other") for item in items)
    file_type = next(iter(categories)) if len(categories) == 1 else "Multiple"
    return {
        "fileId": source["fileId"],
        "courseId": source["courseId"],
        "fileType": file_type,
        "success": True,
        "failReason": None,
    }


def process_partner_classification_task(task_id: str, payload: dict[str, Any]) -> None:
    """Download every CRM file and persist one stable result per ``fileId``."""
    if not _start_partner_task(task_id):
        return
    sources = _flatten_files(payload)
    try:
        with _semaphore():
            llm = QwenClient()
            if not llm.available:
                complete_task(
                    task_id,
                    [_failure(source, "QWEN_API_KEY 未配置，无法执行纯大模型分类") for source in sources],
                )
                return

            settings = get_settings()
            results: list[dict[str, Any] | None] = [None] * len(sources)
            work_items: list[FileItem] = []
            owners: list[int] = []
            with tempfile.TemporaryDirectory(
                prefix=f"partner-task-{task_id[:16]}-", dir=settings.workspace_path
            ) as temp_dir:
                root = Path(temp_dir)

                def download_source(
                    entry: tuple[int, dict[str, str]],
                ) -> tuple[int, Path, Path | None, str]:
                    index, source = entry
                    source_root = root / f"{index:04d}"
                    download_dir = source_root / "download"
                    download_dir.mkdir(parents=True)
                    try:
                        with _download_semaphore():
                            downloaded = _download(
                                source["fileUrl"], download_dir, source["fileName"]
                            )
                        return index, source_root, downloaded, ""
                    except ValueError as exc:
                        return index, source_root, None, str(exc)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "下载文件失败 task=%s file=%s", task_id, source["fileId"]
                        )
                        return index, source_root, None, "文件下载失败"

                with ThreadPoolExecutor(
                    max_workers=_download_concurrency(),
                    thread_name_prefix="partner-download",
                ) as pool:
                    downloaded_sources = list(pool.map(download_source, enumerate(sources)))

                # Only the network download phase is parallel. Archive
                # extraction stays serial to keep CPU and memory usage bounded.
                for index, source_root, downloaded, download_error in downloaded_sources:
                    source = sources[index]
                    if downloaded is None:
                        results[index] = _failure(source, download_error)
                        continue
                    try:
                        documents = _documents(downloaded, source_root)
                        if not documents:
                            raise ValueError("文件或压缩包中没有可分类内容")
                        for document in documents:
                            work_items.append(
                                FileItem(
                                    order_id=f"PARTNER:{task_id}",
                                    original_name=(
                                        source["fileName"]
                                        if len(documents) == 1
                                        else document.name
                                    ),
                                    local_path=str(document),
                                    path_context=(
                                        f"{source['courseName']} / {source['fileName']}"
                                    ),
                                )
                            )
                            owners.append(index)
                    except (ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
                        results[index] = _failure(source, str(exc))
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "展开文件失败 task=%s file=%s", task_id, source["fileId"]
                        )
                        results[index] = _failure(source, "文件读取或解压失败")

                if work_items:
                    classified = asyncio.run(
                        classify_all_async(
                            work_items,
                            llm,
                            use_rules=False,
                            concurrency=min(
                                max(settings.task_classification_concurrency, 1), 16
                            ),
                        )
                    )
                    grouped: dict[int, list[FileItem]] = {}
                    for owner, item in zip(owners, classified, strict=True):
                        grouped.setdefault(owner, []).append(item)
                    for owner, items in grouped.items():
                        failed = [item for item in items if item.classified_by != "llm"]
                        if failed:
                            reason = failed[0].note or "大模型分类失败"
                            results[owner] = _failure(sources[owner], reason)
                        else:
                            results[owner] = _success(sources[owner], items)

            complete_task(
                task_id,
                [
                    result or _failure(source, "任务未能生成分类结果")
                    for source, result in zip(sources, results, strict=True)
                ],
            )
    except Exception:  # noqa: BLE001
        logger.exception("CRM 分类任务执行失败 task=%s", task_id)
        complete_task(task_id, [_failure(source, "任务执行失败，请联系管理员") for source in sources])
    finally:
        _release_partner_task(task_id)
