"""Signed, expiring access tokens for per-user upload links."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from .config import get_settings


class AccessTokenError(ValueError):
    """Base error raised when an upload-link token cannot be accepted."""


class AccessTokenExpired(AccessTokenError):
    """Raised when a correctly signed token is past its expiry time."""


@dataclass(frozen=True)
class AccessIdentity:
    user_id: str
    customer_name: str
    customer_source: str
    staff_id: str
    staff_name: str
    issued_at: int
    expires_at: int
    token_id: str


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _secret(value: str | None = None) -> bytes:
    configured = value if value is not None else get_settings().link_signing_secret
    if len(configured.encode("utf-8")) < 32:
        raise AccessTokenError("LINK_SIGNING_SECRET 未配置或长度不足 32 字节")
    return configured.encode("utf-8")


def normalize_user_id(user_id: str) -> str:
    normalized = user_id.strip()
    if not normalized:
        raise AccessTokenError("用户标识不能为空")
    if len(normalized) > 128 or any(ord(char) < 32 for char in normalized):
        raise AccessTokenError("用户标识格式无效")
    return normalized


def create_access_token(
    user_id: str,
    expires_in_seconds: int,
    *,
    customer_name: str,
    customer_source: str,
    staff_id: str,
    staff_name: str,
    secret: str | None = None,
    now: int | None = None,
) -> tuple[str, AccessIdentity]:
    """Create a signed token and return it with its decoded identity."""
    user_id = normalize_user_id(user_id)
    customer_name = normalize_user_id(customer_name)
    if customer_source not in {"mysql", "temporary"}:
        raise AccessTokenError("客户来源无效")
    staff_id = normalize_user_id(staff_id)
    staff_name = normalize_user_id(staff_name)
    if expires_in_seconds < 1:
        raise AccessTokenError("生效时长必须大于 0")
    issued_at = int(time.time()) if now is None else int(now)
    identity = AccessIdentity(
        user_id=user_id,
        customer_name=customer_name,
        customer_source=customer_source,
        staff_id=staff_id,
        staff_name=staff_name,
        issued_at=issued_at,
        expires_at=issued_at + int(expires_in_seconds),
        token_id=secrets.token_urlsafe(12),
    )
    payload = {
        "sub": identity.user_id,
        "customer_name": identity.customer_name,
        "source": identity.customer_source,
        "sid": identity.staff_id,
        "staff": identity.staff_name,
        "iat": identity.issued_at,
        "exp": identity.expires_at,
        "jti": identity.token_id,
    }
    encoded = _b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    signature = _b64encode(hmac.new(_secret(secret), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}", identity


def validate_access_token(
    token: str,
    *,
    secret: str | None = None,
    now: int | None = None,
) -> AccessIdentity:
    """Verify signature and expiry, returning the user encoded in the token."""
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(_secret(secret), encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise AccessTokenError("访问链接签名无效")
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
        identity = AccessIdentity(
            user_id=normalize_user_id(str(payload["sub"])),
            customer_name=normalize_user_id(str(payload["customer_name"])),
            customer_source=str(payload["source"]),
            staff_id=normalize_user_id(str(payload["sid"])),
            staff_name=normalize_user_id(str(payload["staff"])),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            token_id=str(payload["jti"]),
        )
    except AccessTokenError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise AccessTokenError("访问链接格式无效") from exc

    if identity.customer_source not in {"mysql", "temporary"}:
        raise AccessTokenError("客户来源无效")

    current = int(time.time()) if now is None else int(now)
    if identity.expires_at <= current:
        raise AccessTokenExpired("访问链接已过期")
    if identity.issued_at > current + 300 or identity.expires_at <= identity.issued_at:
        raise AccessTokenError("访问链接时间信息无效")
    return identity
