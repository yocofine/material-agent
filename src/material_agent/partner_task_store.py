"""SQLite persistence for the CRM-to-AI classification contract."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import get_settings


@dataclass(frozen=True)
class PartnerClassificationTask:
    task_id: str
    customer_key: str
    request_payload: dict[str, Any]
    status: str
    results: list[dict[str, Any]]
    created_at: float
    updated_at: float


def _db_path() -> Path:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///") :]
    elif url.startswith("sqlite:"):
        raw = url[len("sqlite:") :]
    else:
        raise ValueError("当前任务存储只支持 SQLite DATABASE_URL")
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(str(_db_path()), timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS partner_classification_tasks (
                task_id TEXT PRIMARY KEY,
                customer_key TEXT NOT NULL,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )


def accept_task(
    task_id: str,
    customer_key: str,
    request_payload: dict[str, Any],
) -> tuple[PartnerClassificationTask, bool]:
    """Atomically accept a task, returning ``created=False`` for a retry."""
    init_db()
    now = time.time()
    encoded_request = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":"))
    with closing(_connect()) as connection, connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO partner_classification_tasks
                (task_id, customer_key, request_json, status, created_at, updated_at)
            VALUES (?, ?, ?, 'PROCESSING', ?, ?)
            """,
            (task_id, customer_key, encoded_request, now, now),
        )
        created = cursor.rowcount == 1
    task = get_task(task_id)
    if task is None:  # pragma: no cover - guarded by the insert above
        raise RuntimeError("任务写入失败")
    return task, created


def complete_task(task_id: str, results: list[dict[str, Any]]) -> None:
    init_db()
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            UPDATE partner_classification_tasks
            SET status = 'DONE', result_json = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (json.dumps(results, ensure_ascii=False), time.time(), task_id),
        )


def get_task(task_id: str) -> PartnerClassificationTask | None:
    init_db()
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT * FROM partner_classification_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        request_payload = json.loads(row["request_json"])
    except (TypeError, json.JSONDecodeError):
        request_payload = {}
    try:
        results = json.loads(row["result_json"])
    except (TypeError, json.JSONDecodeError):
        results = []
    return PartnerClassificationTask(
        task_id=str(row["task_id"]),
        customer_key=str(row["customer_key"]),
        request_payload=request_payload if isinstance(request_payload, dict) else {},
        status=str(row["status"]),
        results=results if isinstance(results, list) else [],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )
