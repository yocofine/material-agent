"""Read-only access to the external MySQL customer/staff relationship table."""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass

from .config import get_settings, is_configured

logger = logging.getLogger(__name__)


class CustomerDatabaseError(RuntimeError):
    pass


class CustomerDatabaseNotConfigured(CustomerDatabaseError):
    pass


@dataclass(frozen=True)
class OfficialCustomer:
    customer_id: str
    customer_name: str
    staff_username: str


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(value: str, label: str) -> str:
    value = value.strip()
    if not _IDENTIFIER.fullmatch(value):
        raise CustomerDatabaseNotConfigured(f"{label} 未配置或不是合法的 MySQL 标识符")
    return f"`{value}`"


class CustomerMySqlRepository:
    def __init__(self) -> None:
        settings = get_settings()
        self.host = settings.customer_db_host.strip()
        self.port = settings.customer_db_port
        self.database = settings.customer_db_name.strip()
        self.user = settings.customer_db_user.strip()
        self.password = settings.customer_db_password
        self.table = settings.customer_db_table.strip()
        self.customer_id_column = settings.customer_id_column.strip()
        self.customer_name_column = settings.customer_name_column.strip()
        self.staff_username_column = settings.staff_username_column.strip()

    @property
    def configured(self) -> bool:
        return all(
            (
                is_configured(self.host),
                bool(self.database),
                bool(self.user),
                bool(self.table),
                bool(self.customer_id_column),
                bool(self.customer_name_column),
                bool(self.staff_username_column),
            )
        )

    def _query_parts(self) -> tuple[str, str, str, str]:
        if not self.configured:
            raise CustomerDatabaseNotConfigured("客户关系 MySQL 尚未完整配置")
        return (
            _identifier(self.table, "CUSTOMER_DB_TABLE"),
            _identifier(self.customer_id_column, "CUSTOMER_ID_COLUMN"),
            _identifier(self.customer_name_column, "CUSTOMER_NAME_COLUMN"),
            _identifier(self.staff_username_column, "STAFF_USERNAME_COLUMN"),
        )

    def _connect(self):  # noqa: ANN202
        if not self.configured:
            raise CustomerDatabaseNotConfigured("客户关系 MySQL 尚未完整配置")
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover
            raise CustomerDatabaseError("缺少 PyMySQL 依赖") from exc
        try:
            return pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                connect_timeout=5,
                read_timeout=10,
                write_timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("无法连接客户关系数据库：%s", exc)
            raise CustomerDatabaseError("正式客户数据暂不可用") from exc

    @staticmethod
    def _row(row: dict) -> OfficialCustomer:
        return OfficialCustomer(
            customer_id=str(row["customer_id"]),
            customer_name=str(row["customer_name"]),
            staff_username=str(row["staff_username"]),
        )

    def list_for_staff(self, staff_username: str) -> list[OfficialCustomer]:
        table, customer_id, customer_name, staff_column = self._query_parts()
        sql = (
            f"SELECT DISTINCT {customer_id} AS customer_id, "
            f"{customer_name} AS customer_name, {staff_column} AS staff_username "
            f"FROM {table} WHERE {staff_column} = %s ORDER BY {customer_name}, {customer_id}"
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(sql, (staff_username,))
                return [self._row(row) for row in cursor.fetchall()]
        except CustomerDatabaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询客户关系失败：%s", exc)
            raise CustomerDatabaseError("正式客户数据暂不可用") from exc

    def find_by_id(self, customer_id_value: str) -> list[OfficialCustomer]:
        table, customer_id, customer_name, staff_column = self._query_parts()
        sql = (
            f"SELECT DISTINCT {customer_id} AS customer_id, "
            f"{customer_name} AS customer_name, {staff_column} AS staff_username "
            f"FROM {table} WHERE {customer_id} = %s"
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(sql, (customer_id_value,))
                return [self._row(row) for row in cursor.fetchall()]
        except CustomerDatabaseError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询客户归属失败：%s", exc)
            raise CustomerDatabaseError("正式客户数据暂不可用") from exc

    def get_for_staff(self, customer_id: str, staff_username: str) -> OfficialCustomer | None:
        normalized = staff_username.casefold()
        return next(
            (
                customer
                for customer in self.find_by_id(customer_id)
                if customer.staff_username.casefold() == normalized
            ),
            None,
        )
