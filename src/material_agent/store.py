"""订单状态持久化。默认 SQLite（零外部依赖），生产可平滑切 Postgres。

表结构：
  orders(order_id PK, status, course_code, assignment_type, student_id,
         student_name, source, work_dir, confirm_approved, payload JSON, updated_at)
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import get_settings
from .models import ExceptionRecord, FileItem, OrderContext, OrderStatus


def _db_path() -> Path:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///") :]
    elif url.startswith("sqlite:"):
        raw = url[len("sqlite:") :]
    else:
        raise ValueError(
            "当前存储实现只支持 SQLite DATABASE_URL；Postgres 需要接入对应的持久化适配器"
        )
    p = Path(raw)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def checkpoint_db_path() -> Path:
    """Return the durable LangGraph checkpoint database path."""
    p = _db_path()
    return p.with_name(f"{p.stem}.checkpoints{p.suffix or '.db'}")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                course_code TEXT,
                assignment_type TEXT,
                student_id TEXT,
                student_name TEXT,
                source TEXT,
                work_dir TEXT,
                confirm_approved INTEGER,
                payload TEXT,
                updated_at REAL
            )
            """
        )


def save_order(order: OrderContext) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO orders (order_id, status, course_code, assignment_type,
                                student_id, student_name, source, work_dir,
                                confirm_approved, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                status=excluded.status,
                course_code=excluded.course_code,
                assignment_type=excluded.assignment_type,
                student_id=excluded.student_id,
                student_name=excluded.student_name,
                source=excluded.source,
                work_dir=excluded.work_dir,
                confirm_approved=excluded.confirm_approved,
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (
                order.order_id,
                order.status.value,
                order.course_code,
                order.assignment_type,
                order.student_id,
                order.student_name,
                order.source,
                order.work_dir,
                order.confirm_approved,
                json.dumps(order.to_dict(), ensure_ascii=False),
                time.time(),
            ),
        )


def load_order(order_id: str) -> OrderContext | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if row is None:
        return None
    data = json.loads(row["payload"])
    order = OrderContext(
        order_id=data["order_id"],
        course_code=data.get("course_code", ""),
        assignment_type=data.get("assignment_type", ""),
        student_id=data.get("student_id", ""),
        student_name=data.get("student_name", ""),
        source=data.get("source", ""),
        work_dir=data.get("work_dir", ""),
        confirm_approved=data.get("confirm_approved"),
        status=OrderStatus(data["status"]),
        files=[FileItem(**f) for f in data.get("files", [])],
        exceptions=[ExceptionRecord(**e) for e in data.get("exceptions", [])],
    )
    return order


def list_orders() -> list[str]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT order_id, status FROM orders ORDER BY updated_at DESC").fetchall()
    return [f"{r['order_id']} [{r['status']}]" for r in rows]
