"""SQLite-backed tutoring staff accounts for server-side access control."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from .config import get_settings


@dataclass(frozen=True)
class StaffAccount:
    staff_id: str
    username: str
    display_name: str
    active: bool
    created_at: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class UserAssignment:
    user_id: str
    staff_id: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StaffSession:
    token_hash: str
    staff_id: str
    is_admin: bool
    expires_at: float


@dataclass(frozen=True)
class TemporaryCustomer:
    internal_id: str
    staff_id: str
    customer_id: str
    customer_name: str
    created_at: float

    def to_dict(self) -> dict:
        return asdict(self)


def _db_path() -> Path:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///") :]
    elif url.startswith("sqlite:"):
        raw = url[len("sqlite:") :]
    else:
        raise ValueError("当前教辅账号存储只支持 SQLite DATABASE_URL")
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(str(_db_path()))
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_accounts (
                staff_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_assignments (
                user_id TEXT PRIMARY KEY COLLATE NOCASE,
                staff_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_assignments_staff ON user_assignments(staff_id)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_sessions (
                token_hash TEXT PRIMARY KEY,
                staff_id TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_staff_sessions_owner ON staff_sessions(staff_id, expires_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS temporary_customers (
                internal_id TEXT PRIMARY KEY,
                staff_id TEXT NOT NULL,
                customer_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
                customer_name TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_temporary_customers_staff ON temporary_customers(staff_id)"
        )


def _normalize_username(username: str) -> str:
    value = username.strip()
    if not 3 <= len(value) <= 64:
        raise ValueError("登录名长度必须为 3 到 64 个字符")
    if not all(char.isalnum() or char in "._-@" for char in value):
        raise ValueError("登录名只能包含文字、数字及 . _ - @")
    return value


def _validate_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("密码长度至少为 10 个字符")
    if len(password) > 256:
        raise ValueError("密码过长")


def _hash_password(password: str) -> str:
    _validate_password(password)
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, expected_hex = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(expected_hex)),
        )
        return hmac.compare_digest(derived.hex(), expected_hex)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_staff(username: str, display_name: str, password: str) -> StaffAccount:
    init_db()
    username = _normalize_username(username)
    display_name = display_name.strip()
    if not display_name or len(display_name) > 128:
        raise ValueError("教辅姓名不能为空且不能超过 128 个字符")
    now = time.time()
    account = StaffAccount(uuid4().hex, username, display_name, True, now)
    try:
        with closing(_connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO staff_accounts
                    (staff_id, username, display_name, password_hash, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (account.staff_id, username, display_name, _hash_password(password), now, now),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("该登录名已存在") from exc
    return account


def list_staff() -> list[StaffAccount]:
    init_db()
    with closing(_connect()) as connection, connection:
        rows = connection.execute(
            """
            SELECT staff_id, username, display_name, active, created_at
            FROM staff_accounts ORDER BY created_at
            """
        ).fetchall()
    return [
        StaffAccount(
            staff_id=row["staff_id"],
            username=row["username"],
            display_name=row["display_name"],
            active=bool(row["active"]),
            created_at=float(row["created_at"]),
        )
        for row in rows
    ]


def get_staff(staff_id: str) -> StaffAccount | None:
    init_db()
    with closing(_connect()) as connection, connection:
        row = connection.execute(
            """
            SELECT staff_id, username, display_name, active, created_at
            FROM staff_accounts WHERE staff_id = ?
            """,
            (staff_id,),
        ).fetchone()
    if row is None:
        return None
    return StaffAccount(
        staff_id=row["staff_id"],
        username=row["username"],
        display_name=row["display_name"],
        active=bool(row["active"]),
        created_at=float(row["created_at"]),
    )


def authenticate_staff(username: str, password: str) -> StaffAccount | None:
    init_db()
    with closing(_connect()) as connection, connection:
        row = connection.execute(
            "SELECT * FROM staff_accounts WHERE username = ? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
    if row is None or not bool(row["active"]) or not _verify_password(password, row["password_hash"]):
        return None
    return StaffAccount(
        staff_id=row["staff_id"],
        username=row["username"],
        display_name=row["display_name"],
        active=True,
        created_at=float(row["created_at"]),
    )


def update_staff(staff_id: str, *, active: bool | None = None, password: str | None = None) -> StaffAccount:
    account = get_staff(staff_id)
    if account is None:
        raise ValueError("教辅账号不存在")
    assignments: list[str] = []
    values: list[object] = []
    if active is not None:
        assignments.append("active = ?")
        values.append(int(active))
    if password is not None:
        assignments.append("password_hash = ?")
        values.append(_hash_password(password))
    if assignments:
        assignments.append("updated_at = ?")
        values.append(time.time())
        values.append(staff_id)
        with closing(_connect()) as connection, connection:
            connection.execute(
                f"UPDATE staff_accounts SET {', '.join(assignments)} WHERE staff_id = ?",
                values,
            )
    updated = get_staff(staff_id)
    if updated is None:  # pragma: no cover
        raise ValueError("教辅账号不存在")
    if password is not None or active is False:
        delete_staff_sessions(staff_id)
    return updated


def change_staff_password(staff_id: str, current_password: str, new_password: str) -> None:
    """Verify the current password, replace it, and revoke every active session."""
    init_db()
    _validate_password(new_password)
    with closing(_connect()) as connection, connection:
        row = connection.execute(
            "SELECT password_hash FROM staff_accounts WHERE staff_id = ? AND active = 1",
            (staff_id,),
        ).fetchone()
        if row is None or not _verify_password(current_password, row["password_hash"]):
            raise ValueError("当前密码不正确")
        if _verify_password(new_password, row["password_hash"]):
            raise ValueError("新密码不能与当前密码相同")
        connection.execute(
            "UPDATE staff_accounts SET password_hash = ?, updated_at = ? WHERE staff_id = ?",
            (_hash_password(new_password), time.time(), staff_id),
        )
    delete_staff_sessions(staff_id)


def create_staff_session(
    staff_id: str,
    *,
    is_admin: bool,
    ttl_seconds: int = 43200,
    now: float | None = None,
) -> str:
    init_db()
    if ttl_seconds < 1:
        raise ValueError("会话有效期必须大于 0")
    token = secrets.token_urlsafe(48)
    created_at = time.time() if now is None else float(now)
    with closing(_connect()) as connection, connection:
        connection.execute("DELETE FROM staff_sessions WHERE expires_at <= ?", (created_at,))
        connection.execute(
            """
            INSERT INTO staff_sessions (token_hash, staff_id, is_admin, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_token_hash(token), staff_id, int(is_admin), created_at + ttl_seconds, created_at),
        )
    return token


def get_staff_session(token: str) -> StaffSession | None:
    if not token:
        return None
    init_db()
    now = time.time()
    with closing(_connect()) as connection, connection:
        row = connection.execute(
            "SELECT token_hash, staff_id, is_admin, expires_at FROM staff_sessions WHERE token_hash = ?",
            (_token_hash(token),),
        ).fetchone()
        if row is not None and float(row["expires_at"]) <= now:
            connection.execute("DELETE FROM staff_sessions WHERE token_hash = ?", (row["token_hash"],))
            return None
    if row is None:
        return None
    return StaffSession(
        token_hash=row["token_hash"],
        staff_id=row["staff_id"],
        is_admin=bool(row["is_admin"]),
        expires_at=float(row["expires_at"]),
    )


def delete_staff_session(token: str) -> None:
    if not token:
        return
    init_db()
    with closing(_connect()) as connection, connection:
        connection.execute("DELETE FROM staff_sessions WHERE token_hash = ?", (_token_hash(token),))


def delete_staff_sessions(staff_id: str) -> None:
    init_db()
    with closing(_connect()) as connection, connection:
        connection.execute("DELETE FROM staff_sessions WHERE staff_id = ?", (staff_id,))


def create_temporary_customer(staff_id: str, customer_id: str, customer_name: str) -> TemporaryCustomer:
    init_db()
    customer_id = _normalize_user_id(customer_id)
    customer_name = customer_name.strip()
    if not customer_name or len(customer_name) > 128 or any(ord(char) < 32 for char in customer_name):
        raise ValueError("客户名称不能为空且不能超过 128 个字符")
    customer = TemporaryCustomer(uuid4().hex, staff_id, customer_id, customer_name, time.time())
    try:
        with closing(_connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO temporary_customers
                    (internal_id, staff_id, customer_id, customer_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    customer.internal_id,
                    customer.staff_id,
                    customer.customer_id,
                    customer.customer_name,
                    customer.created_at,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("该客户编号已存在") from exc
    return customer


def list_temporary_customers(staff_id: str) -> list[TemporaryCustomer]:
    init_db()
    with closing(_connect()) as connection, connection:
        rows = connection.execute(
            """
            SELECT internal_id, staff_id, customer_id, customer_name, created_at
            FROM temporary_customers WHERE staff_id = ? ORDER BY created_at DESC
            """,
            (staff_id,),
        ).fetchall()
    return [
        TemporaryCustomer(
            row["internal_id"],
            row["staff_id"],
            row["customer_id"],
            row["customer_name"],
            float(row["created_at"]),
        )
        for row in rows
    ]


def get_temporary_customer(customer_id: str) -> TemporaryCustomer | None:
    init_db()
    customer_id = _normalize_user_id(customer_id)
    with closing(_connect()) as connection, connection:
        row = connection.execute(
            """
            SELECT internal_id, staff_id, customer_id, customer_name, created_at
            FROM temporary_customers WHERE customer_id = ? COLLATE NOCASE
            """,
            (customer_id,),
        ).fetchone()
    if row is None:
        return None
    return TemporaryCustomer(
        row["internal_id"],
        row["staff_id"],
        row["customer_id"],
        row["customer_name"],
        float(row["created_at"]),
    )


def _normalize_user_id(user_id: str) -> str:
    value = user_id.strip()
    if not value or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise ValueError("用户名称或编号格式无效")
    return value


def get_user_assignment(user_id: str) -> UserAssignment | None:
    init_db()
    user_id = _normalize_user_id(user_id)
    with closing(_connect()) as connection, connection:
        row = connection.execute(
            "SELECT user_id, staff_id, created_at, updated_at FROM user_assignments WHERE user_id = ? COLLATE NOCASE",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return UserAssignment(
        user_id=row["user_id"],
        staff_id=row["staff_id"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def assign_user(user_id: str, staff_id: str, *, allow_reassign: bool = False) -> UserAssignment:
    """Create the unique user owner, optionally transferring it as an admin action."""
    init_db()
    user_id = _normalize_user_id(user_id)
    now = time.time()
    with closing(_connect()) as connection, connection:
        row = connection.execute(
            "SELECT user_id, staff_id, created_at, updated_at FROM user_assignments WHERE user_id = ? COLLATE NOCASE",
            (user_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO user_assignments (user_id, staff_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, staff_id, now, now),
            )
            return UserAssignment(user_id, staff_id, now, now)
        if row["staff_id"] != staff_id:
            if not allow_reassign:
                raise ValueError("该用户已经分配给其他教辅")
            connection.execute(
                "UPDATE user_assignments SET staff_id = ?, updated_at = ? WHERE user_id = ? COLLATE NOCASE",
                (staff_id, now, user_id),
            )
            return UserAssignment(row["user_id"], staff_id, float(row["created_at"]), now)
        return UserAssignment(
            row["user_id"], row["staff_id"], float(row["created_at"]), float(row["updated_at"])
        )
