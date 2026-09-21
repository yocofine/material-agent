"""HTTP API for uploading files and receiving classification results.

Start with::

    python -m material_agent.api_server

Then POST multipart files to ``/api/classify``. The endpoint is deliberately
separate from the WeCom bridge so it can be used by a browser, frontend, or
another internal service without requiring a WeCom configuration.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from dataclasses import dataclass, replace
from uuid import uuid4
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .access_tokens import (
    AccessIdentity,
    AccessTokenError,
    AccessTokenExpired,
    create_access_token,
    validate_access_token,
)
from .archive_utils import (
    ArchiveFileCountExceeded,
    ArchiveSizeLimitExceeded,
    extract_rar_safe,
    extract_tar_safe,
    extract_zip_recursive,
    safe_filename,
)
from .classifier import classify_all_async
from .config import get_settings
from .customer_mysql import CustomerDatabaseError, CustomerMySqlRepository
from .llm import QwenClient
from .models import FileItem
from .staff_store import (
    authenticate_staff,
    change_staff_password,
    create_staff,
    create_staff_session,
    create_temporary_customer,
    delete_staff_session,
    get_staff,
    get_staff_session,
    get_temporary_customer,
    list_staff,
    list_temporary_customers,
    update_staff,
)
from .web_pages import ADMIN_HTML, LOGIN_HTML, STAFF_HTML
from .task_service import process_classification_task, redact_url
from .task_store import create_task, get_task
from .partner_task_service import process_partner_classification_task, reserve_partner_task
from .partner_task_store import accept_task as accept_partner_task
from .partner_task_store import get_task as get_partner_task

logger = logging.getLogger(__name__)


class _HealthCheckAccessFilter(logging.Filter):
    """Hide repetitive health-probe access lines without hiding real traffic."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            request_path = str(args[2]).partition("?")[0]
            if request_path in {"/health", "/material-ai/health"}:
                return False
        return True


# Uvicorn configures this logger before importing the ASGI application, so a
# logger-level filter works both for ``uvicorn ...`` and ``python -m`` starts.
logging.getLogger("uvicorn.access").addFilter(_HealthCheckAccessFilter())

app = FastAPI(title="material-agent classification API", version="0.1.0")
SESSION_COOKIE = "material_agent_session"


class ClassificationTaskRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_url: HttpUrl = Field(alias="fileUrl")


class PartnerFileRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    file_id: str = Field(alias="fileId", min_length=1, max_length=128)
    file_name: str = Field(alias="fileName", min_length=1, max_length=500)
    file_url: HttpUrl = Field(alias="fileUrl")


class PartnerCourseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    course_id: str = Field(alias="courseId", min_length=1, max_length=128)
    course_name: str = Field(alias="courseName", min_length=1, max_length=500)
    files: list[PartnerFileRequest] = Field(min_length=1)


class PartnerClassificationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    task_id: str = Field(alias="taskId", min_length=1, max_length=128)
    customer_key: str = Field(alias="customerKey", min_length=1, max_length=128)
    courses: list[PartnerCourseRequest] = Field(min_length=1)

    @model_validator(mode="after")
    def file_ids_must_be_unique(self) -> "PartnerClassificationRequest":
        file_ids = [file.file_id for course in self.courses for file in course.files]
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("同一任务中的 fileId 不能重复")
        return self


def _partner_payload(payload: PartnerClassificationRequest, *, redact: bool) -> dict:
    data = payload.model_dump(by_alias=True, mode="json")
    if redact:
        for course in data["courses"]:
            for file_data in course["files"]:
                file_data["fileUrl"] = redact_url(file_data["fileUrl"])
    return data


def _check_task_api(request: Request) -> None:
    configured = get_settings().task_api_token
    if not configured:
        return
    authorization = request.headers.get("Authorization", "")
    prefix = "Bearer "
    supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
    if not supplied or not hmac.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail="任务接口认证失败")


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    if request.url.path in {
        "/material-ai/api/classify",
        "/material-ai/api/result",
        "/api/ai/classify",
        "/api/ai/result",
    }:
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": "请求参数无效", "data": None},
        )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """统一把未处理异常转成 JSON，避免前端拿到非 JSON 的 500 文本。"""
    logger.exception("未处理异常：%s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请查看服务端日志"},
    )

SUPPORTED_DOCUMENTS = {
    ".pdf",
    ".docx",
    ".ppt",
    ".pptx",
    ".txt",
    ".md",
    ".html",
    ".htm",
    ".csv",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
}
SUPPORTED_ARCHIVES = {".zip", ".tar", ".tar.gz", ".tgz", ".rar"}
DEFAULT_MAX_FILES = 100
DEFAULT_MAX_FILE_BYTES = 500 * 1024 * 1024
# 源码运行指向项目根目录；打包成 exe 后指向 exe 所在目录，便于读写 .env
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _suffix(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".tar.gz"):
        return ".tar.gz"
    return Path(lowered).suffix


def _extract_archive(
    archive: Path,
    destination: Path,
    max_total_bytes: int | None = None,
    max_files: int | None = None,
) -> None:
    suffix = _suffix(archive.name)
    if suffix == ".zip":
        extract_zip_recursive(
            archive, destination, max_total_bytes=max_total_bytes, max_files=max_files
        )
    elif suffix in {".tar", ".tar.gz", ".tgz"}:
        extract_tar_safe(
            archive, destination, max_total_bytes=max_total_bytes, max_files=max_files
        )
    elif suffix == ".rar":
        extract_rar_safe(
            archive, destination, max_total_bytes=max_total_bytes, max_files=max_files
        )
    else:  # pragma: no cover - guarded by callers
        raise ValueError(f"不支持的压缩格式：{archive.name}")


def _public_result(item: FileItem, archive_path: str = "") -> dict:
    """Return classification data without exposing server paths or document text."""
    return {
        "filename": item.original_name,
        "path": item.path_context,
        "category": item.category,
        "confidence": item.confidence,
        "classified_by": item.classified_by,
        "matched_keyword": item.matched_keyword,
        "needs_confirmation": item.needs_confirmation,
        "timed_out": item.timed_out,
        "note": item.note,
        "archive_path": archive_path,
        "prompt_tokens": item.prompt_tokens,
        "completion_tokens": item.completion_tokens,
        "total_tokens": item.total_tokens,
        "elapsed_ms": item.elapsed_ms,
    }


def _archive_group(path: Path, root: Path) -> str:
    """如果该批次包含压缩包，则以压缩包名字建一层目录；否则直接按分类归档。"""
    extracted_root = root / "extracted"
    archive_dirs: list[str] = []
    if extracted_root.is_dir():
        archive_dirs = [
            p.name for p in extracted_root.iterdir() if p.is_dir()
        ]
    if archive_dirs:
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == "extracted" and len(relative.parts) > 1:
            return safe_filename(relative.parts[1], fallback=archive_dirs[0])
        # 压缩包和文字/直接上传文件一起归档到同一个压缩包目录
        return safe_filename(archive_dirs[0], fallback="archive")
    return ""


def _unique_destination(directory: Path, filename: str) -> Path:
    destination = directory / safe_filename(filename)
    if not destination.exists():
        return destination
    stem = destination.stem
    suffix = destination.suffix
    index = 2
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _archive_classified_file(
    item: FileItem,
    root: Path,
    archive_root: Path,
    *,
    staff_id: str,
    user_id: str,
) -> str:
    """Copy one file into a staff/user-isolated classification tree."""
    group = _archive_group(Path(item.local_path), root)
    category = safe_filename(item.category or "Other", fallback="Other")
    staff_component = safe_filename(staff_id, fallback="unknown-staff")
    user_component = safe_filename(user_id, fallback="unknown-user")
    destination_dir = archive_root / staff_component / user_component / group / category
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = _unique_destination(destination_dir, item.original_name)
    shutil.copy2(item.local_path, destination)
    return str(destination.relative_to(archive_root))


async def _save_upload(upload: UploadFile, destination: Path, max_bytes: int) -> Path:
    filename = safe_filename(upload.filename or "file.bin")
    target = destination / filename
    if target.exists():
        stem, suffix = target.stem, target.suffix
        index = 2
        while target.exists():
            target = destination / f"{stem}_{index}{suffix}"
            index += 1

    total = 0
    too_large = False
    try:
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    too_large = True
                    break
                output.write(chunk)
    finally:
        # On Windows the temporary upload must be closed before unlinking it.
        await upload.close()

    if too_large:
        target.unlink(missing_ok=True)
        raise HTTPException(
            status_code=413,
            detail=f"文件过大：{upload.filename}，单文件上限为 {max_bytes // (1024 * 1024)} MB",
        )
    return target


def _collect_documents(root: Path) -> list[Path]:
    """收集所有待处理文件。

    支持文档正常分类；规则以外的文件格式也会保留并归入 Other，
    压缩包本身不参与分类（其内容会先被解压出来）。
    """
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and _suffix(p.name) not in SUPPORTED_ARCHIVES
    )


def _pending_batches() -> list[Path]:
    inbox = get_settings().inbox_path
    return sorted(
        (p for p in inbox.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _processed_batches() -> list[Path]:
    root = get_settings().workspace_path / "processed"
    if not root.is_dir():
        return []
    return [path for path in root.iterdir() if path.is_dir()]


def _customer_activity(staff_id: str) -> dict[tuple[str, str], dict[str, float | int]]:
    activity: dict[tuple[str, str], dict[str, float | int]] = {}
    pending_paths = _pending_batches()
    for batch in [*pending_paths, *_processed_batches()]:
        metadata = _batch_metadata(batch)
        if metadata.get("staff_id") != staff_id:
            continue
        customer_id = str(metadata.get("user_id", ""))
        source = str(metadata.get("customer_source", ""))
        if not customer_id or source not in {"mysql", "temporary"}:
            continue
        key = (source, customer_id.casefold())
        item = activity.setdefault(key, {"pending_batch_count": 0, "last_upload_at": 0.0})
        if batch in pending_paths:
            item["pending_batch_count"] = int(item["pending_batch_count"]) + 1
        uploaded_at = float(metadata.get("uploaded_at") or batch.stat().st_mtime)
        item["last_upload_at"] = max(float(item["last_upload_at"]), uploaded_at)
    return activity


def _prepare_batch_documents(batch_dir: Path) -> list[Path]:
    """解压该批次里的压缩包，然后返回所有待分类文档。"""
    raw_dir = batch_dir / "raw"
    extracted_dir = batch_dir / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    max_extracted_bytes = max(1, get_settings().api_max_extracted_mb) * 1024 * 1024
    max_extracted_files = max(1, get_settings().api_max_extracted_files)
    extracted_bytes = 0
    extracted_files = 0

    if raw_dir.is_dir():
        for archive in sorted(raw_dir.rglob("*")):
            if not archive.is_file() or _suffix(archive.name) not in SUPPORTED_ARCHIVES:
                continue
            dest = extracted_dir / archive.stem
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True, exist_ok=True)
            remaining_bytes = max_extracted_bytes - extracted_bytes
            remaining_files = max_extracted_files - extracted_files
            if remaining_bytes <= 0:
                raise ArchiveSizeLimitExceeded(
                    f"压缩包解压后总大小超过 {get_settings().api_max_extracted_mb} MB"
                )
            if remaining_files <= 0:
                raise ArchiveFileCountExceeded(
                    f"压缩包解压后文件数不能超过 {max_extracted_files} 个"
                )
            try:
                _extract_archive(
                    archive,
                    dest,
                    max_total_bytes=remaining_bytes,
                    max_files=remaining_files,
                )
            except Exception:
                shutil.rmtree(dest, ignore_errors=True)
                raise
            extracted_bytes += sum(
                path.stat().st_size for path in dest.rglob("*") if path.is_file()
            )
            extracted_files += sum(1 for path in dest.rglob("*") if path.is_file())

    return _collect_documents(batch_dir)


@dataclass(frozen=True)
class StaffPrincipal:
    staff_id: str
    username: str
    display_name: str
    is_admin: bool = False


def _authenticate_request(request: Request) -> StaffPrincipal:
    """Authenticate a server-side session stored in an HttpOnly cookie."""
    session = get_staff_session(request.cookies.get(SESSION_COOKIE, ""))
    if session is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    settings = get_settings()
    if session.is_admin:
        if not settings.admin_password or session.staff_id != "__admin__":
            raise HTTPException(status_code=401, detail="管理员会话已失效")
        return StaffPrincipal("__admin__", settings.admin_username, "超级管理员", True)
    account = get_staff(session.staff_id)
    if account is None or not account.active:
        raise HTTPException(status_code=401, detail="教辅账号已停用或不存在")
    return StaffPrincipal(account.staff_id, account.username, account.display_name, False)


def _optional_principal(request: Request) -> StaffPrincipal | None:
    try:
        return _authenticate_request(request)
    except HTTPException:
        return None


def _visible_batches(principal: StaffPrincipal) -> list[Path]:
    batches = _pending_batches()
    if principal.is_admin:
        return batches
    return [
        batch
        for batch in batches
        if _batch_metadata(batch).get("staff_id") == principal.staff_id
    ]


def _require_staff(request: Request) -> StaffPrincipal:
    return _authenticate_request(request)


def _check_admin(request: Request) -> StaffPrincipal:
    principal = _authenticate_request(request)
    if not principal.is_admin:
        raise HTTPException(
            status_code=403,
            detail="只有超级管理员可以执行此操作",
        )
    return principal


def _require_tutor(request: Request) -> StaffPrincipal:
    principal = _authenticate_request(request)
    if principal.is_admin:
        raise HTTPException(status_code=403, detail="该操作仅供教辅账号使用")
    return principal


def _require_access(token: str | None) -> AccessIdentity:
    """Validate the token embedded in a user's private upload link."""
    if not token:
        raise HTTPException(status_code=401, detail="请使用管理员发送的专属上传链接")
    try:
        identity = validate_access_token(token)
        account = get_staff(identity.staff_id)
        if account is None or not account.active:
            raise AccessTokenError("负责该用户的教辅账号已停用")
        if identity.customer_source == "mysql":
            customer = CustomerMySqlRepository().get_for_staff(identity.user_id, account.username)
            if customer is None:
                raise AccessTokenError("该客户的负责教辅已经变更")
            return replace(identity, customer_name=customer.customer_name)
        customer = get_temporary_customer(identity.user_id)
        if customer is None or customer.staff_id != identity.staff_id:
            raise AccessTokenError("临时客户不存在或归属已经变更")
        return replace(identity, customer_name=customer.customer_name)
    except AccessTokenExpired as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except AccessTokenError as exc:
        status = 503 if "LINK_SIGNING_SECRET" in str(exc) else 403
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except CustomerDatabaseError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _batch_metadata(batch_dir: Path) -> dict:
    path = batch_dir / "metadata.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _json_for_script(value: object) -> str:
    """Serialize JSON safely for embedding inside an HTML script block."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


@app.get("/", response_class=HTMLResponse)
def upload_page(token: str | None = Query(None)) -> HTMLResponse:
    """外部用户上传页；专属链接中的 token 即为访问凭证。"""
    identity = _require_access(token)
    html_path = Path(__file__).resolve().parent / "static" / "upload.html"
    try:
        html = html_path.read_text(encoding="utf-8")
        html = html.replace("__UPLOAD_USER_JSON__", _json_for_script(identity.user_id))
        html = html.replace("__UPLOAD_CUSTOMER_NAME_JSON__", _json_for_script(identity.customer_name))
        html = html.replace("__UPLOAD_EXPIRES_AT__", str(identity.expires_at))
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )
    except FileNotFoundError:
        return HTMLResponse(
            """<!doctype html><html><body><h1>上传页缺失</h1><p>请检查 static/upload.html 是否存在。</p></body></html>""",
            status_code=500,
        )


@app.get("/material-ai/health")
@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/material-ai/api/classify")
@app.post("/api/ai/classify", include_in_schema=False)
def submit_partner_classification(
    payload: PartnerClassificationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Accept the CRM-owned task ID and process all files asynchronously."""
    _check_task_api(request)
    stored_payload = _partner_payload(payload, redact=True)
    task, _ = accept_partner_task(
        payload.task_id,
        payload.customer_key,
        stored_payload,
    )
    if task.status == "PROCESSING" and reserve_partner_task(payload.task_id):
        background_tasks.add_task(
            process_partner_classification_task,
            payload.task_id,
            _partner_payload(payload, redact=False),
        )
    return {
        "code": 0,
        "msg": "ok",
        "data": {"accepted": True, "taskId": payload.task_id},
    }


@app.get("/material-ai/api/result")
@app.get("/api/ai/result", include_in_schema=False)
def query_partner_classification(
    request: Request,
    task_id: Annotated[
        str,
        Query(alias="taskId", min_length=1, max_length=128),
    ],
) -> dict:
    """Return the stable CRM result using the required ``?taskId=`` query."""
    _check_task_api(request)
    task = get_partner_task(task_id)
    if task is None:
        data = {"taskId": task_id, "status": "NOT_FOUND", "results": None}
    else:
        data = {
            "taskId": task.task_id,
            "status": task.status,
            "results": task.results if task.status == "DONE" else None,
        }
    return {"code": 0, "msg": "ok", "data": data}


@app.post("/api/tasks", status_code=202)
def submit_classification_task(
    payload: ClassificationTaskRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Submit one HTTP/HTTPS file URL and return immediately with a task ID."""
    _check_task_api(request)
    file_url = str(payload.file_url)
    task = create_task(redact_url(file_url))
    background_tasks.add_task(process_classification_task, task.task_id, file_url)
    return {"taskId": task.task_id, "status": task.status}


@app.get("/api/tasks/{task_id}")
def query_classification_task(task_id: str, request: Request) -> dict:
    """Query task status and its eventual classification result."""
    _check_task_api(request)
    if len(task_id) != 32 or any(character not in "0123456789abcdef" for character in task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task.to_public_dict()


@app.post("/api/upload")
async def upload_files(request: Request, token: str | None = Query(None)) -> dict:
    """外部用户上传入口：只保存文件/文字，不立即分类。"""
    identity = _require_access(token)
    form = await request.form()
    files = form.getlist("files")
    text = form.get("text")
    has_text = bool(text and str(text).strip())

    if not files and not has_text:
        raise HTTPException(status_code=400, detail="至少上传一个文件或输入一段文字")
    if len(files) > DEFAULT_MAX_FILES:
        raise HTTPException(status_code=413, detail=f"一次最多上传 {DEFAULT_MAX_FILES} 个文件")

    settings = get_settings()
    upload_id = uuid4().hex[:12]
    batch_dir = settings.inbox_path / upload_id
    raw_dir = batch_dir / "raw"
    text_dir = batch_dir / "text_input"
    raw_dir.mkdir(parents=True)
    text_dir.mkdir(parents=True)

    max_file_mb = max(1, int(settings.api_max_file_mb or DEFAULT_MAX_FILE_BYTES // (1024 * 1024)))
    max_file_bytes = max_file_mb * 1024 * 1024
    max_archive_bytes = max(1, settings.api_max_archive_mb) * 1024 * 1024
    archive_bytes = 0
    saved_count = 0
    saved_files: list[str] = []
    text_file: str | None = None
    try:
        for upload in files:
            upload_name = safe_filename(upload.filename or "file.bin")
            is_archive = _suffix(upload_name) in SUPPORTED_ARCHIVES
            file_limit = min(max_file_bytes, max_archive_bytes) if is_archive else max_file_bytes
            saved = await _save_upload(upload, raw_dir, file_limit)
            if is_archive:
                archive_bytes += saved.stat().st_size
                if archive_bytes > max_archive_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"单次上传的压缩包总大小不能超过 {settings.api_max_archive_mb} MB",
                    )
            saved_files.append(saved.name)
            saved_count += 1
        if has_text:
            md_name = f"input_{uuid4().hex[:12]}.md"
            (text_dir / md_name).write_text(str(text), encoding="utf-8")
            text_file = md_name
            saved_count += 1
        (batch_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "user_id": identity.user_id,
                    "customer_name": identity.customer_name,
                    "customer_source": identity.customer_source,
                    "staff_id": identity.staff_id,
                    "staff_name": identity.staff_name,
                    "token_id": identity.token_id,
                    "link_expires_at": identity.expires_at,
                    "uploaded_at": int(time.time()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise

    return {
        "ok": True,
        "upload_id": upload_id,
        "count": saved_count,
        "saved_files": saved_files,
        "text_file": text_file,
        "user_id": identity.user_id,
        "message": f"上传成功，共接收 {saved_count} 个文件/文字",
    }


@app.get("/api/pending")
def pending_uploads(request: Request) -> dict:
    principal = _require_staff(request)
    batches = _visible_batches(principal)
    return {
        "ok": True,
        "count": len(batches),
        "items": [
            {
                "upload_id": p.name,
                "created": p.stat().st_mtime,
                "files": len(_collect_documents(p)),
                "user_id": _batch_metadata(p).get("user_id", ""),
                "customer_name": _batch_metadata(p).get("customer_name", ""),
                "customer_source": _batch_metadata(p).get("customer_source", ""),
                "staff_id": _batch_metadata(p).get("staff_id", ""),
                "staff_name": _batch_metadata(p).get("staff_name", ""),
            }
            for p in batches
        ],
    }


@app.post("/api/classify_pending")
async def classify_pending(
    request: Request,
    upload_id: str | None = Query(None, description="指定批次 ID；不传则分类全部待处理批次"),
    llm_only: bool = Query(True, description="默认跳过规则，所有文件都交给 LLM"),
    concurrency: int = Query(20, ge=1, le=50, description="同时分类的文件数"),
) -> dict:
    """服务端一键分类：处理 inbox 中的待分类上传，归档到 ORGANIZED_DIR。"""
    principal = _require_staff(request)
    settings = get_settings()
    if upload_id:
        if any(ch in upload_id for ch in "/\\") or ".." in upload_id:
            raise HTTPException(status_code=400, detail="非法的批次 ID")
        batch_dir = settings.inbox_path / upload_id
        if not batch_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"批次不存在：{upload_id}")
        metadata = _batch_metadata(batch_dir)
        if not principal.is_admin and metadata.get("staff_id") != principal.staff_id:
            raise HTTPException(status_code=404, detail=f"批次不存在：{upload_id}")
        batches = [batch_dir]
    else:
        batches = _visible_batches(principal)

    if not batches:
        return {"ok": True, "count": 0, "message": "没有待分类的上传"}

    processed_dir = settings.workspace_path / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    all_results: list[dict] = []
    all_files = 0
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    started = time.perf_counter()

    for batch_dir in batches:
        metadata = _batch_metadata(batch_dir)
        try:
            documents = _prepare_batch_documents(batch_dir)
        except (ArchiveSizeLimitExceeded, ArchiveFileCountExceeded) as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except (ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise HTTPException(status_code=400, detail=f"压缩包无效：{exc}") from exc
        if not documents:
            continue
        items = [
            FileItem(
                order_id=f"UPLOAD:{batch_dir.name}",
                original_name=path.name,
                local_path=str(path),
                path_context=str(path.relative_to(batch_dir)),
            )
            for path in documents
        ]
        results = await classify_all_async(
            items,
            QwenClient(),
            use_rules=not llm_only,
            concurrency=concurrency,
        )
        for item in results:
            archive_path = _archive_classified_file(
                item,
                batch_dir,
                settings.organized_path,
                staff_id=str(metadata.get("staff_id") or "legacy"),
                user_id=str(metadata.get("user_id") or "legacy"),
            )
            all_results.append(_public_result(item, archive_path))
            all_files += 1
            prompt_tokens += item.prompt_tokens
            completion_tokens += item.completion_tokens
            total_tokens += item.total_tokens

        # 分类成功后将原始批次移到 processed，避免下次重复处理
        target = _unique_destination(processed_dir, batch_dir.name)
        shutil.move(str(batch_dir), str(target))

    return {
        "ok": True,
        "count": all_files,
        "message": f"分类完成，共处理 {all_files} 个文件",
        "archive_root": str(settings.organized_path),
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "elapsed_ms": max(0, round((time.perf_counter() - started) * 1000)),
        "results": all_results,
        "staff_id": principal.staff_id,
    }


@app.get("/api/admin/settings")
def admin_settings(request: Request) -> dict:
    _check_admin(request)
    s = get_settings()
    return {"ok": True, "organized_dir": str(s.organized_dir), "inbox_dir": str(s.inbox_dir)}


@app.post("/api/admin/settings")
def update_admin_settings(request: Request, payload: dict) -> dict:
    _check_admin(request)
    new_path = str(payload.get("organized_dir", "")).strip()
    if not new_path:
        raise HTTPException(status_code=400, detail="保存路径不能为空")

    env_path = PROJECT_ROOT / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("ORGANIZED_DIR="):
            lines[i] = f"ORGANIZED_DIR={new_path}"
            found = True
            break
    if not found:
        lines.append(f"ORGANIZED_DIR={new_path}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    get_settings.cache_clear()
    return {"ok": True, "organized_dir": str(get_settings().organized_path)}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):  # noqa: ANN201
    principal = _optional_principal(request)
    if principal is not None:
        return RedirectResponse("/admin" if principal.is_admin else "/staff", status_code=303)
    return HTMLResponse(LOGIN_HTML, headers={"Cache-Control": "no-store"})


@app.post("/api/auth/login")
def login(payload: dict) -> JSONResponse:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    settings = get_settings()
    is_admin = bool(
        settings.admin_password
        and hmac.compare_digest(username, settings.admin_username)
        and hmac.compare_digest(password, settings.admin_password)
    )
    account = None if is_admin else authenticate_staff(username, password)
    if not is_admin and account is None:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    staff_id = "__admin__" if is_admin else account.staff_id
    ttl_seconds = min(max(settings.staff_session_hours, 1), 168) * 3600
    token = create_staff_session(staff_id, is_admin=is_admin, ttl_seconds=ttl_seconds)
    response = JSONResponse(
        {"ok": True, "redirect": "/admin" if is_admin else "/staff"}
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=ttl_seconds,
        httponly=True,
        secure=settings.public_base_url.lower().startswith("https://"),
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def logout(request: Request) -> JSONResponse:
    delete_staff_session(request.cookies.get(SESSION_COOKIE, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/staff", response_class=HTMLResponse)
def staff_page(request: Request):  # noqa: ANN201
    principal = _optional_principal(request)
    if principal is None:
        return RedirectResponse("/login", status_code=303)
    if principal.is_admin:
        return RedirectResponse("/admin", status_code=303)
    return HTMLResponse(STAFF_HTML, headers={"Cache-Control": "no-store"})


@app.get("/api/auth/me")
def current_staff(request: Request) -> dict:
    principal = _require_staff(request)
    return {
        "staff_id": principal.staff_id,
        "username": principal.username,
        "display_name": principal.display_name,
        "is_admin": principal.is_admin,
    }


@app.patch("/api/staff/me/password")
def change_my_password(request: Request, payload: dict) -> JSONResponse:
    principal = _require_tutor(request)
    try:
        change_staff_password(
            principal.staff_id,
            str(payload.get("current_password", "")),
            str(payload.get("new_password", "")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = JSONResponse({"ok": True, "message": "密码已修改，请重新登录"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/admin/staff")
def admin_list_staff(request: Request) -> dict:
    _check_admin(request)
    return {"ok": True, "items": [account.to_dict() for account in list_staff()]}


@app.post("/api/admin/staff")
def admin_create_staff(request: Request, payload: dict) -> dict:
    _check_admin(request)
    username = str(payload.get("username", ""))
    if username.strip().casefold() == get_settings().admin_username.casefold():
        raise HTTPException(status_code=400, detail="登录名不能与超级管理员相同")
    try:
        account = create_staff(
            username,
            str(payload.get("display_name", "")),
            str(payload.get("password", "")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "staff": account.to_dict()}


@app.patch("/api/admin/staff/{staff_id}")
def admin_update_staff(staff_id: str, request: Request, payload: dict) -> dict:
    _check_admin(request)
    active = payload.get("active") if "active" in payload else None
    if active is not None and not isinstance(active, bool):
        raise HTTPException(status_code=400, detail="active 必须是布尔值")
    password = str(payload["password"]) if payload.get("password") else None
    try:
        account = update_staff(
            staff_id,
            active=active,
            password=password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "staff": account.to_dict()}


@app.get("/api/staff/customers")
def staff_customers(request: Request) -> dict:
    principal = _require_tutor(request)
    activity = _customer_activity(principal.staff_id)
    items: list[dict] = []
    official_available = True
    official_error = ""
    try:
        official = CustomerMySqlRepository().list_for_staff(principal.username)
        for customer in official:
            stats = activity.get(("mysql", customer.customer_id.casefold()), {})
            items.append(
                {
                    "customer_id": customer.customer_id,
                    "customer_name": customer.customer_name,
                    "customer_source": "mysql",
                    "pending_batch_count": int(stats.get("pending_batch_count", 0)),
                    "last_upload_at": float(stats.get("last_upload_at", 0)),
                }
            )
    except CustomerDatabaseError as exc:
        official_available = False
        official_error = str(exc)

    for customer in list_temporary_customers(principal.staff_id):
        stats = activity.get(("temporary", customer.customer_id.casefold()), {})
        items.append(
            {
                "customer_id": customer.customer_id,
                "customer_name": customer.customer_name,
                "customer_source": "temporary",
                "pending_batch_count": int(stats.get("pending_batch_count", 0)),
                "last_upload_at": float(stats.get("last_upload_at", 0)),
            }
        )
    items.sort(key=lambda item: (item["customer_name"].casefold(), item["customer_id"].casefold()))
    return {
        "ok": True,
        "official_available": official_available,
        "official_error": official_error,
        "items": items,
    }


@app.post("/api/staff/customers/temporary")
def create_staff_temporary_customer(request: Request, payload: dict) -> dict:
    principal = _require_tutor(request)
    customer_id = str(payload.get("customer_id", "")).strip()
    repository = CustomerMySqlRepository()
    try:
        if repository.find_by_id(customer_id):
            raise HTTPException(status_code=409, detail="该编号已经属于正式客户")
    except CustomerDatabaseError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        customer = create_temporary_customer(
            principal.staff_id,
            customer_id,
            str(payload.get("customer_name", "")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "customer": customer.to_dict()}


@app.post("/api/staff/access-links")
def create_user_access_link(request: Request, payload: dict) -> dict:
    """Generate a link only for an official or temporary customer owned by this tutor."""
    principal = _require_tutor(request)
    try:
        hours = float(payload.get("expires_in_hours", 24))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="生效时长必须是数字") from exc
    if hours < (1 / 60) or hours > 8760:
        raise HTTPException(status_code=400, detail="生效时长必须在 1 分钟到 365 天之间")
    customer_id = str(payload.get("customer_id", "")).strip()
    source = str(payload.get("customer_source", "")).strip()
    if source == "mysql":
        try:
            official = CustomerMySqlRepository().get_for_staff(customer_id, principal.username)
        except CustomerDatabaseError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if official is None:
            raise HTTPException(status_code=403, detail="该正式客户不属于当前教辅")
        customer_name = official.customer_name
    elif source == "temporary":
        temporary = get_temporary_customer(customer_id)
        if temporary is None or temporary.staff_id != principal.staff_id:
            raise HTTPException(status_code=403, detail="该临时客户不属于当前教辅")
        customer_name = temporary.customer_name
    else:
        raise HTTPException(status_code=400, detail="customer_source 必须是 mysql 或 temporary")
    try:
        token, identity = create_access_token(
            customer_id,
            max(1, round(hours * 3600)),
            customer_name=customer_name,
            customer_source=source,
            staff_id=principal.staff_id,
            staff_name=principal.display_name,
        )
    except AccessTokenError as exc:
        status = 503 if "LINK_SIGNING_SECRET" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    path = f"/?token={token}"
    public_base = get_settings().public_base_url.strip().rstrip("/")
    return {
        "ok": True,
        "token": token,
        "path": path,
        "url": f"{public_base}{path}" if public_base else path,
        "user_id": identity.user_id,
        "customer_id": identity.user_id,
        "customer_name": identity.customer_name,
        "customer_source": identity.customer_source,
        "staff_id": identity.staff_id,
        "staff_name": identity.staff_name,
        "issued_at": identity.issued_at,
        "expires_at": identity.expires_at,
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):  # noqa: ANN201
    """Render the administrator-only dashboard."""
    principal = _optional_principal(request)
    if principal is None:
        return RedirectResponse("/login", status_code=303)
    if not principal.is_admin:
        return RedirectResponse("/staff", status_code=303)
    return HTMLResponse(ADMIN_HTML, headers={"Cache-Control": "no-store"})

    # Legacy inline page retained temporarily below; unreachable after the
    # dedicated page module above and removable in a later cleanup.
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>服务端管理</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #f4f6f8; color: #20252b; margin: 0; padding: 24px; }
    .card { max-width: 800px; margin: 0 auto; background: #fff; border: 1px solid #dfe3e8; border-radius: 8px; padding: 24px; }
    h1 { margin-top: 0; }
    label { display: block; margin: 12px 0 4px; font-weight: 600; }
    input[type=text] { width: 100%; box-sizing: border-box; padding: 8px; border: 1px solid #b8c0ca; border-radius: 6px; }
    button { margin-top: 12px; padding: 10px 18px; border: 0; border-radius: 6px; background: #1769e0; color: #fff; cursor: pointer; }
    button.secondary { background: #667085; }
    #pending { margin-top: 20px; }
    #message { margin-top: 12px; min-height: 24px; }
    #results { margin-top: 20px; }
    #results table { width: 100%; border-collapse: collapse; }
    #results th, #results td { border-bottom: 1px solid #e8ebee; padding: 8px 6px; text-align: left; vertical-align: top; }
    .success { color: #067647; }
    .error { color: #b42318; }
  </style>
</head>
<body>
<div class="card">
  <h1>服务端管理</h1>
  <p id="identity"></p>

  <section id="adminSettings">
  <h2>分类保存路径</h2>
  <label for="path">归档目录（ORGANIZED_DIR）</label>
  <input id="path" type="text" placeholder="例如 D:\\classified">
  <button id="savePath" type="button">保存路径</button>
  </section>

  <h2>生成用户专属上传链接</h2>
  <div id="staffChoice" style="display:none">
    <label for="staffId">负责教辅</label>
    <select id="staffId"></select>
    <label><input id="reassignUser" type="checkbox">若用户已有教辅，确认转交给所选教辅</label>
  </div>
  <label for="userId">用户名称或编号</label>
  <input id="userId" type="text" placeholder="例如 student-001">
  <label for="expiresHours">有效时长（小时）</label>
  <input id="expiresHours" type="number" min="0.0167" max="8760" step="0.5" value="24">
  <button id="createLink" type="button">生成并复制链接</button>
  <div id="linkResult"></div>

  <section id="staffManagement" style="display:none">
    <h2>教辅账号</h2>
    <label for="staffUsername">登录名</label>
    <input id="staffUsername" type="text">
    <label for="staffName">教辅姓名</label>
    <input id="staffName" type="text">
    <label for="staffPassword">初始密码（至少 10 位）</label>
    <input id="staffPassword" type="password">
    <button id="createStaff" type="button">创建教辅账号</button>
    <div id="staffList"></div>
  </section>

  <h2>待分类上传</h2>
  <div id="pending">加载中...</div>
  <label><input id="llmOnly" type="checkbox">全部使用大模型</label>
  <button id="classifyBtn" class="secondary" type="button">一键分类全部</button>
  <div id="message"></div>
  <div id="results"></div>
</div>
<script>
const pathInput = document.getElementById('path');
const pendingDiv = document.getElementById('pending');
const messageDiv = document.getElementById('message');
const resultsDiv = document.getElementById('results');
const principal = __PRINCIPAL_JSON__;
document.getElementById('identity').textContent = `当前登录：${principal.display_name}（${principal.username}）`;
if (principal.is_admin) {
  document.getElementById('staffChoice').style.display = '';
  document.getElementById('staffManagement').style.display = '';
} else {
  document.getElementById('adminSettings').style.display = 'none';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}

async function refresh() {
  const pendingRes = await fetch('/api/pending');
  const pending = await pendingRes.json();
  if (principal.is_admin) {
    const settingsRes = await fetch('/api/admin/settings');
    const settings = await settingsRes.json();
    pathInput.value = settings.organized_dir || '';
  }
  if (!pending.count) {
    pendingDiv.textContent = '当前没有待分类上传。';
  } else {
    pendingDiv.textContent = `当前有 ${pending.count} 个待分类批次：`;
    const ul = document.createElement('ul');
    for (const item of pending.items) {
      const li = document.createElement('li');
      const owner = item.staff_name ? `${item.staff_name} / ` : '';
      li.textContent = `${owner}${item.user_id || '旧版未标记用户'} / ${item.upload_id}（${item.files} 个文档）`;
      ul.appendChild(li);
    }
    pendingDiv.appendChild(ul);
  }
}

async function loadStaff() {
  if (!principal.is_admin) return;
  const res = await fetch('/api/admin/staff');
  const data = await res.json();
  const select = document.getElementById('staffId');
  select.innerHTML = '';
  const list = document.getElementById('staffList');
  list.innerHTML = '';
  for (const staff of data.items || []) {
    if (staff.active) {
      const option = document.createElement('option');
      option.value = staff.staff_id;
      option.textContent = `${staff.display_name}（${staff.username}）`;
      select.appendChild(option);
    }
    const row = document.createElement('div');
    const label = document.createElement('span');
    label.textContent = `${staff.display_name}（${staff.username}）— ${staff.active ? '启用' : '停用'} `;
    const toggle = document.createElement('button');
    toggle.textContent = staff.active ? '停用' : '启用';
    toggle.addEventListener('click', async () => {
      await fetch(`/api/admin/staff/${encodeURIComponent(staff.staff_id)}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({active: !staff.active})
      });
      await loadStaff();
    });
    row.appendChild(label);
    row.appendChild(toggle);
    list.appendChild(row);
  }
}

document.getElementById('createLink').addEventListener('click', async () => {
  const payload = {
    user_id: document.getElementById('userId').value.trim(),
    expires_in_hours: Number(document.getElementById('expiresHours').value)
  };
  if (principal.is_admin) {
    payload.staff_id = document.getElementById('staffId').value;
    payload.reassign = document.getElementById('reassignUser').checked;
  }
  const res = await fetch('/api/staff/access-links', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (!res.ok) { document.getElementById('linkResult').textContent = data.detail || '生成失败'; return; }
  const link = data.url.startsWith('/') ? location.origin + data.url : data.url;
  await navigator.clipboard.writeText(link);
  document.getElementById('linkResult').textContent = `${data.staff_name} / ${data.user_id}：${link}`;
});

document.getElementById('createStaff').addEventListener('click', async () => {
  const payload = {
    username: document.getElementById('staffUsername').value.trim(),
    display_name: document.getElementById('staffName').value.trim(),
    password: document.getElementById('staffPassword').value
  };
  const res = await fetch('/api/admin/staff', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (!res.ok) { messageDiv.className = 'error'; messageDiv.textContent = data.detail || '创建失败'; return; }
  messageDiv.className = 'success'; messageDiv.textContent = '教辅账号已创建';
  document.getElementById('staffPassword').value = '';
  await loadStaff();
});

document.getElementById('savePath').addEventListener('click', async () => {
  const value = pathInput.value.trim();
  if (!value) { messageDiv.className = 'error'; messageDiv.textContent = '路径不能为空'; return; }
  const res = await fetch('/api/admin/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({organized_dir: value})
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || '保存失败');
  messageDiv.className = 'success';
  messageDiv.textContent = `保存路径已更新：${data.organized_dir}`;
});

document.getElementById('classifyBtn').addEventListener('click', async () => {
  messageDiv.className = '';
  messageDiv.textContent = '正在分类，请稍候...';
  resultsDiv.innerHTML = '';
  const query = document.getElementById('llmOnly').checked ? '?llm_only=true' : '';
  const res = await fetch('/api/classify_pending' + query, {method: 'POST'});
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { throw new Error(text || '服务器返回异常'); }
  if (!res.ok) throw new Error(data.detail || '分类失败');
  messageDiv.className = 'success';
  messageDiv.textContent = data.message + `，归档目录：${data.archive_root}`;

  const items = data.results || [];
  if (items.length) {
    let html = '<h3>分类结果</h3><table><thead><tr><th>文件</th><th>分类</th><th>状态</th><th>归档位置</th></tr></thead><tbody>';
    for (const item of items) {
      html += `<tr><td>${escapeHtml(item.filename)}</td><td>${escapeHtml(item.category || 'Other')}</td>`;
      html += `<td>${item.needs_confirmation ? '待确认' : '已分类'}</td>`;
      html += `<td>${escapeHtml(item.archive_path || '-')}</td></tr>`;
    }
    html += '</tbody></table>';
    resultsDiv.innerHTML = html;
  }

  await refresh();
});

loadStaff();
refresh();
</script>
</body>
</html>"""
    return html.replace(
        "__PRINCIPAL_JSON__",
        _json_for_script(
            {
                "staff_id": principal.staff_id,
                "username": principal.username,
                "display_name": principal.display_name,
                "is_admin": principal.is_admin,
            },
        ),
    )


@app.post("/api/classify")
async def classify_uploads(
    request: Request,
    token: str | None = Query(None),
    llm_only: bool = Query(True, description="默认跳过规则，所有文件都交给 LLM"),
    concurrency: int = Query(20, ge=1, le=50, description="同时分类的文件数"),
) -> dict:
    """Upload files and/or plain text, then classify them together.

    Text is converted server-side into a randomly named Markdown file so it
    goes through the same classification and archiving pipeline as uploaded
    documents. The request is synchronous from the caller's perspective: the
    response is returned after all files have been classified. Temporary files
    are removed in ``finally`` even when extraction or LLM classification
    fails.
    """
    identity = _require_access(token)
    form = await request.form()
    files = form.getlist("files")
    text = form.get("text")
    has_text = bool(text and str(text).strip())

    if not files and not has_text:
        raise HTTPException(status_code=400, detail="至少上传一个文件或输入一段文字")
    if len(files) > DEFAULT_MAX_FILES:
        raise HTTPException(status_code=413, detail=f"一次最多上传 {DEFAULT_MAX_FILES} 个文件")

    settings = get_settings()
    request_started = time.perf_counter()
    max_file_mb = max(1, int(settings.api_max_file_mb or DEFAULT_MAX_FILE_BYTES // (1024 * 1024)))
    max_file_bytes = max_file_mb * 1024 * 1024
    max_archive_bytes = max(1, settings.api_max_archive_mb) * 1024 * 1024
    max_extracted_bytes = max(1, settings.api_max_extracted_mb) * 1024 * 1024
    max_extracted_files = max(1, settings.api_max_extracted_files)
    with tempfile.TemporaryDirectory(prefix="material-agent-api-", dir=settings.workspace_path) as raw_dir:
        root = Path(raw_dir)
        upload_dir = root / "uploads"
        extracted_dir = root / "extracted"
        text_dir = root / "text_input"
        upload_dir.mkdir()
        extracted_dir.mkdir()
        text_dir.mkdir()

        try:
            archive_bytes = 0
            extracted_bytes = 0
            extracted_files = 0
            for upload in files:
                upload_name = safe_filename(upload.filename or "file.bin")
                is_archive = _suffix(upload_name) in SUPPORTED_ARCHIVES
                file_limit = min(max_file_bytes, max_archive_bytes) if is_archive else max_file_bytes
                saved = await _save_upload(upload, upload_dir, file_limit)
                suffix = _suffix(saved.name)
                if suffix in SUPPORTED_ARCHIVES:
                    archive_bytes += saved.stat().st_size
                    if archive_bytes > max_archive_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"单次上传的压缩包总大小不能超过 {settings.api_max_archive_mb} MB",
                        )
                    archive_destination = extracted_dir / saved.stem
                    archive_destination.mkdir(parents=True, exist_ok=True)
                    remaining_bytes = max_extracted_bytes - extracted_bytes
                    remaining_files = max_extracted_files - extracted_files
                    if remaining_bytes <= 0:
                        raise ArchiveSizeLimitExceeded(
                            f"压缩包解压后总大小超过 {settings.api_max_extracted_mb} MB"
                        )
                    if remaining_files <= 0:
                        raise ArchiveFileCountExceeded(
                            f"压缩包解压后文件数不能超过 {max_extracted_files} 个"
                        )
                    _extract_archive(
                        saved,
                        archive_destination,
                        max_total_bytes=remaining_bytes,
                        max_files=remaining_files,
                    )
                    extracted_bytes += sum(
                        path.stat().st_size
                        for path in archive_destination.rglob("*")
                        if path.is_file()
                    )
                    extracted_files += sum(
                        1 for path in archive_destination.rglob("*") if path.is_file()
                    )

            # 把输入文字转成随机命名的 Markdown 文件，与上传文件一起走同一套分类/归档流程
            if has_text:
                md_name = f"input_{uuid4().hex[:12]}.md"
                (text_dir / md_name).write_text(str(text), encoding="utf-8")

            document_paths = _collect_documents(root)
            if not document_paths:
                raise HTTPException(
                    status_code=400,
                    detail="没有找到可分类的文档，支持 pdf/docx/ppt/pptx/txt/md/html/csv/xlsx/图片",
                )

            items = [
                FileItem(
                    order_id="API",
                    original_name=path.name,
                    local_path=str(path),
                    path_context=str(path.relative_to(root)),
                )
                for path in document_paths
            ]
            results = await classify_all_async(
                items,
                QwenClient(),
                use_rules=not llm_only,
                concurrency=concurrency,
            )
            archive_root = settings.organized_path
            public_results = []
            for item in results:
                archive_path = _archive_classified_file(
                    item,
                    root,
                    archive_root,
                    staff_id=identity.staff_id,
                    user_id=identity.user_id,
                )
                public_results.append(_public_result(item, archive_path))
            usage = {
                "prompt_tokens": sum(item.prompt_tokens for item in results),
                "completion_tokens": sum(item.completion_tokens for item in results),
                "total_tokens": sum(item.total_tokens for item in results),
            }
            elapsed_ms = max(0, round((time.perf_counter() - request_started) * 1000))
            return {
                "ok": True,
                "count": len(results),
                "archive_root": str(archive_root),
                "usage": usage,
                "elapsed_ms": elapsed_ms,
                "user_id": identity.user_id,
                "results": public_results,
            }
        except HTTPException:
            raise
        except (ArchiveSizeLimitExceeded, ArchiveFileCountExceeded) as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except (ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            logger.warning("上传归档处理失败：%s", exc)
            raise HTTPException(status_code=400, detail=f"文件或压缩包无效：{exc}") from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("文件分类接口失败")
            raise HTTPException(status_code=500, detail="文件分类失败，请查看服务端日志") from exc


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import uvicorn

    host = os.environ.get("API_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=8000)


if __name__ == "__main__":
    main()
