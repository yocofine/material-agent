"""SQLite persistence for minimal asynchronous classification tasks."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import get_settings


@dataclass(frozen=True)
class ClassificationTask:
    task_id: str
    source_url: str
    status: str
    result_type: str
    summary: str
    items: list[dict[str, Any]]
    error: str
    created_at: float
    updated_at: float

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "status": self.status,
            "type": self.result_type or None,
            "summary": self.summary or None,
            "items": self.items,
            "error": self.error or None,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


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
            CREATE TABLE IF NOT EXISTS classification_tasks (
                task_id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                status TEXT NOT NULL,
                result_type TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )


def create_task(source_url: str) -> ClassificationTask:
    init_db()
    now = time.time()
    task = ClassificationTask(uuid4().hex, source_url, "queued", "", "", [], "", now, now)
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO classification_tasks
                (task_id, source_url, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task.task_id, task.source_url, task.status, now, now),
        )
    return task


def update_task(
    task_id: str,
    *,
    status: str,
    result_type: str = "",
    summary: str = "",
    items: list[dict[str, Any]] | None = None,
    error: str = "",
) -> None:
    init_db()
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            UPDATE classification_tasks
            SET status = ?, result_type = ?, summary = ?, result_json = ?, error = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                status,
                result_type,
                summary,
                json.dumps(items or [], ensure_ascii=False),
                error,
                time.time(),
                task_id,
            ),
        )


def get_task(task_id: str) -> ClassificationTask | None:
    init_db()
    with closing(_connect()) as connection, connection:
        row = connection.execute(
            "SELECT * FROM classification_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        items = json.loads(row["result_json"])
    except json.JSONDecodeError:
        items = []
    return ClassificationTask(
        task_id=row["task_id"],
        source_url=row["source_url"],
        status=row["status"],
        result_type=row["result_type"],
        summary=row["summary"],
        items=items if isinstance(items, list) else [],
        error=row["error"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )
