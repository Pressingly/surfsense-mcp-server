"""Stdio-mode auth: SurfSense JWT (env paste or password fallback).

The cache + concurrent-login serialization plumbing is generic and lives
in :class:`moneta_mcp_auth.stdio.PasswordTokenCache`. This module owns
the SurfSense-specific pieces:

- ``SURFSENSE_JWT`` env var (primary path — user pastes a fresh token
  when one expires; the server never refreshes it).
- ``SURFSENSE_EMAIL`` + ``SURFSENSE_PASSWORD`` fallback that exchanges
  the credentials for a JWT via ``POST /auth/jwt/login`` on the SurfSense
  backend.
- The ``TOKEN_TTL`` env var that sets the password cache's TTL.

The module-level singleton :data:`_password_cache` is lazily constructed
on first password-mode call so importing the module doesn't try to set
up the cache before env vars are configured.
"""

from __future__ import annotations

import os

import httpx
from fastmcp.utilities.logging import get_logger
from moneta_mcp_auth.stdio import PasswordTokenCache

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_TOKEN_TTL_SECONDS = 3300

_password_cache: PasswordTokenCache | None = None


def _base_url() -> str:
    base_url = os.getenv("SURFSENSE_BASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("SURFSENSE_BASE_URL is not configured")
    return base_url


def _token_ttl_seconds() -> int:
    raw = os.getenv("TOKEN_TTL")
    if not raw:
        return _DEFAULT_TOKEN_TTL_SECONDS
    try:
        return max(60, int(raw))
    except ValueError:
        return _DEFAULT_TOKEN_TTL_SECONDS


def _has_password_creds() -> bool:
    return bool(os.getenv("SURFSENSE_EMAIL")) and bool(os.getenv("SURFSENSE_PASSWORD"))


async def _login_with_password() -> str:
    """Exchange SURFSENSE_EMAIL + SURFSENSE_PASSWORD for a JWT via fastapi-users."""
    email = os.getenv("SURFSENSE_EMAIL", "")
    password = os.getenv("SURFSENSE_PASSWORD", "")
    if not email or not password:
        raise RuntimeError("Password-login fallback requires SURFSENSE_EMAIL and SURFSENSE_PASSWORD.")

    url = f"{_base_url()}/auth/jwt/login"
    # Lazy import — ``surfsense_mcp.client`` imports ``surfsense_mcp.auth``
    # (this package) at module load, so this would otherwise be a cycle.
    from surfsense_mcp.client import _ssl_verify

    async with httpx.AsyncClient(
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        verify=_ssl_verify(),
    ) as client:
        response = await client.post(
            url,
            data={"username": email, "password": password},
        )
    if response.status_code != 200:
        raise RuntimeError(f"SurfSense password login failed: {response.status_code} {response.text[:200]}")
    body = response.json()
    token = body.get("access_token")
    if not token:
        raise RuntimeError("SurfSense password login returned no access_token")
    logger.info("Authenticated with SurfSense via password (TTL %ds)", _token_ttl_seconds())
    return token


def _get_password_cache() -> PasswordTokenCache:
    """Lazy-build the module-level cache so import has no side effects."""
    global _password_cache
    if _password_cache is None:
        _password_cache = PasswordTokenCache(_login_with_password, ttl_seconds=_token_ttl_seconds())
    return _password_cache


def invalidate_cache() -> None:
    """Drop the cached password token so the next call forces a fresh login.

    Also discards the cache instance itself so a subsequent
    :func:`_get_password_cache` call rebuilds it. This matters for tests
    that monkeypatch :func:`_login_with_password` between cases — without
    the instance reset the cache would still hold a closure over the
    original function reference.
    """
    global _password_cache
    _password_cache = None


def is_password_in_use() -> bool:
    """True iff stdio is currently relying on password-fallback.

    Used by the dispatcher to gate the 401-retry-once path: env-JWT and
    HTTP modes don't benefit from a retry, only the password cache does.
    """
    if os.getenv("SURFSENSE_JWT"):
        return False
    return _has_password_creds()


async def resolve_jwt() -> str:
    """Return a SurfSense JWT for stdio mode.

    Resolution order:
      1. ``SURFSENSE_JWT`` env var (primary, paste-based path)
      2. Password fallback — cached token if fresh, otherwise log in and cache.

    Raises ``RuntimeError`` with an actionable message if neither is configured.
    """
    env_token = os.getenv("SURFSENSE_JWT", "")
    if env_token:
        return env_token

    if _has_password_creds():
        return await _get_password_cache().get()

    raise RuntimeError(
        "No SurfSense credential available. Set SURFSENSE_JWT, "
        "or SURFSENSE_EMAIL + SURFSENSE_PASSWORD for stdio fallback, "
        "or connect via HTTP so AWSCognitoProvider can validate the Cognito Bearer."
    )
