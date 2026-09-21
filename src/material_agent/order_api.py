"""订单系统 API 客户端。

你已确认「订单系统有上传接口」，此处实现了契约与 HTTP 调用骨架，
请把 TODO 处的路径/字段名替换成真实接口定义。
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import get_settings, is_configured

logger = logging.getLogger(__name__)


class OrderApiClient:
    def __init__(self) -> None:
        s = get_settings()
        self.base_url = s.order_api_base_url.rstrip("/")
        self.token = s.order_api_token
        self._client = httpx.Client(timeout=60)

    @property
    def available(self) -> bool:
        # The checked-in .env may contain example values. Treat those as
        # offline configuration so local runs do not call a fake endpoint.
        return is_configured(self.base_url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # ------------------------------------------------------------------ #
    # 上传附件（第③阶段核心）
    # ------------------------------------------------------------------ #
    def upload_attachment(
        self,
        order_id: str,
        student_id: str,
        file_path: str,
        target_name: str,
        category: str,
    ) -> dict:
        """上传单个文件到订单附件区，返回系统响应。

        TODO: 按真实接口调整
          - URL 路径（示例假设为 /orders/{order_id}/attachments）
          - 字段名（示例使用 files= 与 data={name, category, student_id}）
        """
        if not self.available:
            # 未配置订单系统时静默模拟成功，便于离线跑通其余流程
            logger.warning("[订单系统未配置] 模拟上传成功：%s -> %s", Path(file_path).name, target_name)
            return {"ok": True, "simulated": True}

        url = f"{self.base_url}/orders/{quote(order_id, safe='')}/attachments"
        with open(file_path, "rb") as f:
            resp = self._client.post(
                url,
                headers=self._headers(),
                files={"file": (target_name, f)},
                data={"name": target_name, "category": category, "student_id": student_id},
            )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # 必传项校验支持
    # ------------------------------------------------------------------ #
    def get_required_categories(self, order_id: str) -> list[str] | None:
        """拉取该订单应上传的必传项类别清单。

        TODO: 若订单系统有「必传项配置」接口则调用；没有则返回 None，
              由本地 constants.REQUIRED_CATEGORIES 兜底。
        """
        if not self.available:
            return None
        try:
            url = f"{self.base_url}/orders/{quote(order_id, safe='')}/required-categories"
            resp = self._client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json().get("categories")
        except Exception as e:  # noqa: BLE001
            logger.warning("拉取必传项配置失败，退回本地默认：%s", e)
            return None
