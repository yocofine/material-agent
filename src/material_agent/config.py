"""集中配置。所有敏感项从环境变量 / .env 读取。"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# 源码运行：项目根目录；打包 exe：exe 所在目录
if getattr(sys, "frozen", False):
    _APP_ROOT = Path(sys.executable).resolve().parent
else:
    _APP_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _APP_ROOT / ".env"


def _resolve_path(value: str) -> Path:
    """相对路径统一基于应用目录解析，避免因启动目录不同而写错位置。"""
    p = Path(value)
    if p.is_absolute():
        return p
    return (_APP_ROOT / p).resolve()


_PLACEHOLDER_MARKERS = (
    "your-",
    "your_",
    "example.com",
    "access_token=xxx",
    "xxxxxxxx",
)


def is_configured(value: str) -> bool:
    """Return whether an environment value looks usable rather than templated."""
    normalized = value.strip().lower()
    if not normalized:
        return False
    return not any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    # LLM
    qwen_api_key: str = ""
    qwen_model: str = "qwen3.7-plus"
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_timeout: int = 60          # 单次 LLM 调用超时（秒）
    extract_timeout: int = 60       # 单个文件文本抽取超时（秒）
    api_max_file_mb: int = 1024     # 上传分类接口的单文件大小上限
    api_max_archive_mb: int = 300   # 单次上传压缩包原始体积合计上限
    api_max_extracted_mb: int = 600 # 单批次解压后文件体积合计上限
    api_max_extracted_files: int = 199  # 解压后最多 199 个文件（严格小于 200）
    task_api_token: str = ""
    task_download_timeout: int = 120
    task_download_concurrency: int = 3
    task_worker_concurrency: int = 3
    task_classification_concurrency: int = 5
    task_allow_private_urls: bool = False

    # 订单系统
    order_api_base_url: str = ""
    order_api_token: str = ""
    order_api_username: str = ""
    order_api_password: str = ""

    # 超级管理员与网页登录会话
    admin_username: str = "admin"
    admin_password: str = ""
    link_signing_secret: str = ""
    public_base_url: str = ""
    staff_session_hours: int = 12

    # 外部客户—教辅关系表（只读 MySQL）
    customer_db_host: str = ""
    customer_db_port: int = 3306
    customer_db_name: str = ""
    customer_db_user: str = ""
    customer_db_password: str = ""
    customer_db_table: str = ""
    customer_id_column: str = ""
    customer_name_column: str = ""
    staff_username_column: str = ""

    # 存储与工作区
    database_url: str = "sqlite:///./data/material_agent.db"
    workspace_dir: str = "./data/workspace"
    organized_dir: str = "./data/organized"
    inbox_dir: str = "./data/inbox"

    @property
    def workspace_path(self) -> Path:
        p = _resolve_path(self.workspace_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def organized_path(self) -> Path:
        p = _resolve_path(self.organized_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def inbox_path(self) -> Path:
        p = _resolve_path(self.inbox_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
