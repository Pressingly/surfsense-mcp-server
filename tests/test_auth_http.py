"""Unit tests for ``surfsense_mcp.auth.http``.

These cover the relay helpers in isolation. End-to-end behavior (the dispatcher
in :mod:`surfsense_mcp.auth` actually injecting the identity header / Bearer
into a SurfSense request) is covered by ``tests/test_http_mode_auth.py``; the
provider that populates ``upstream_claims`` is covered by
``tests/test_auth_cognito.py``.
"""

from __future__ import annotations

import time

import pytest
from fastmcp.server.auth.auth import AccessToken
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from surfsense_mcp.auth import http as http_auth


def _set_request_token(token: AccessToken):
    return auth_context_var.set(AuthenticatedUser(token))


def _reset_request_token(reset_token) -> None:
    auth_context_var.reset(reset_token)


def _make_token(claims: dict, *, token: str = "cognito-access-token-zzz") -> AccessToken:
    return AccessToken(
        token=token,
        client_id="mcp-client",
        scopes=["openid"],
        expires_at=int(time.time() + 3600),
        claims=claims,
    )


def _upstream(email: str | None = None, cognito_username: str | None = None, id_token: str | None = None) -> dict:
    out: dict[str, str] = {}
    if email is not None:
        out["email"] = email
    if cognito_username is not None:
        out["cognito:username"] = cognito_username
    if id_token is not None:
        out["id_token"] = id_token
    return out


def test_request_token_returns_none_outside_request_scope() -> None:
    """Stdio mode never has a FastMCP HTTP scope; ``get_access_token()``
    raises and the wrapper must swallow it to return ``None``."""
    assert http_auth.request_token() is None


def test_request_token_returns_access_token_inside_scope() -> None:
    token = _make_token({"sub": "abc-1234", "username": "alice"})
    reset = _set_request_token(token)
    try:
        assert http_auth.request_token() is token
    finally:
        _reset_request_token(reset)


# --- upstream_claims --------------------------------------------------------


def test_upstream_claims_empty_when_absent() -> None:
    assert http_auth.upstream_claims(_make_token({"username": "alice"})) == {}


def test_upstream_claims_empty_when_wrong_type() -> None:
    """A non-dict ``upstream_claims`` (corrupt/forged-then-rejected) is ignored."""
    assert http_auth.upstream_claims(_make_token({"upstream_claims": "nope"})) == {}


def test_upstream_claims_returns_embedded_dict() -> None:
    up = _upstream(email="u@x.ai", cognito_username="u", id_token="idtok")
    assert http_auth.upstream_claims(_make_token({"upstream_claims": up})) == up


# --- identity_header (HTTP-direct path) ------------------------------------


def test_identity_header_prefers_email_from_upstream_claims() -> None:
    """The id_token email is what the web flow + SurfSense key on — use it
    first so the MCP user matches the browser-login user."""
    token = _make_token(
        {
            "sub": "09daf50c-uuid",
            "username": "09daf50c-uuid",  # access-token username is the opaque UUID
            "upstream_claims": _upstream(email="1020010000020127@askii.ai", cognito_username="1020010000020127"),
        }
    )
    assert http_auth.identity_header(token) == {"X-Auth-Request-Email": "1020010000020127@askii.ai"}


def test_identity_header_uses_cognito_username_when_no_email() -> None:
    token = _make_token(
        {"sub": "uuid", "username": "uuid", "upstream_claims": _upstream(cognito_username="1020010000020127")}
    )
    assert http_auth.identity_header(token) == {"X-Auth-Request-User": "1020010000020127"}


def test_identity_header_falls_back_to_access_token_username() -> None:
    """No upstream id_token (older token / native user) → use the access-token
    ``username`` claim, preserving the pre-fix behavior for native users."""
    token = _make_token({"sub": "abc-1234", "username": "alice"})
    assert http_auth.identity_header(token) == {"X-Auth-Request-User": "alice"}


def test_identity_header_raises_when_no_identity_anywhere() -> None:
    """Missing everywhere is a hard error — silently sending no header would let
    the backend auto-provision under the wrong identity."""
    token = _make_token({"sub": "abc-1234"})
    with pytest.raises(RuntimeError, match="cannot identify"):
        http_auth.identity_header(token)


def test_identity_header_raises_when_username_empty_string() -> None:
    token = _make_token({"sub": "abc-1234", "username": ""})
    with pytest.raises(RuntimeError, match="cannot identify"):
        http_auth.identity_header(token)


def test_identity_header_raises_when_username_wrong_type() -> None:
    """A claim provider that hands us a non-string username (e.g. None or a
    list) should raise rather than coerce — the wrong type usually means a
    pool-config problem the operator needs to fix."""
    token = _make_token({"sub": "abc-1234", "username": ["alice"]})
    with pytest.raises(RuntimeError, match="cannot identify"):
        http_auth.identity_header(token)


def test_identity_header_raises_when_claims_dict_empty() -> None:
    token = _make_token({})
    with pytest.raises(RuntimeError, match="cannot identify"):
        http_auth.identity_header(token)


# --- bearer_header (HTTPS / mPass path) ------------------------------------


def test_bearer_header_forwards_id_token_when_present() -> None:
    """oauth2-proxy needs email/cognito:username claims → forward the id_token,
    not the access token (which carries neither)."""
    token = _make_token({"username": "uuid", "upstream_claims": _upstream(id_token="the-id-token-jwt")})
    assert http_auth.bearer_header(token) == {"Authorization": "Bearer the-id-token-jwt"}


def test_bearer_header_falls_back_to_access_token_without_id_token() -> None:
    """No id_token (older token) → forward the raw access token. Misidentifies
    federated users, but it's the documented fallback and is logged."""
    token = _make_token({"sub": "abc-1234", "username": "alice"})
    assert http_auth.bearer_header(token) == {"Authorization": "Bearer cognito-access-token-zzz"}


def test_bearer_header_raises_when_no_id_token_and_no_raw_token() -> None:
    """Nothing to forward at all → fail loudly rather than send a malformed
    Authorization header."""
    token = _make_token({"username": "alice"}, token="")
    with pytest.raises(RuntimeError, match="no upstream id_token and no raw token string"):
        http_auth.bearer_header(token)


# --- auth_headers_for_token (scheme dispatcher) ----------------------------


def test_auth_headers_for_token_picks_bearer_for_https(monkeypatch) -> None:
    """HTTPS base URL → request will traverse Traefik + mPass; identity must
    ride ``Authorization`` (the id_token) because Traefik strips ``X-Auth-Request-*``."""
    monkeypatch.setenv("SURFSENSE_BASE_URL", "https://foss-research.local.moneta.dev")
    token = _make_token({"username": "uuid", "upstream_claims": _upstream(id_token="the-id-token-jwt")})
    assert http_auth.auth_headers_for_token(token) == {"Authorization": "Bearer the-id-token-jwt"}


def test_auth_headers_for_token_picks_identity_for_http(monkeypatch) -> None:
    """HTTP base URL → direct docker-network call, no Traefik in the path; we
    inject the identity header ourselves and skip the (irrelevant) Bearer."""
    monkeypatch.setenv("SURFSENSE_BASE_URL", "http://surfsense-backend:8000")
    token = _make_token({"upstream_claims": _upstream(email="1020010000020127@askii.ai")})
    assert http_auth.auth_headers_for_token(token) == {"X-Auth-Request-Email": "1020010000020127@askii.ai"}


def test_auth_headers_for_token_defaults_to_identity_when_unset(monkeypatch) -> None:
    """No URL configured at all → fall through to ``identity_header``. The
    base-URL validator in ``client._base_url`` will fail the call later — at
    this layer we just want predictable behavior, not surprise bearers."""
    monkeypatch.delenv("SURFSENSE_BASE_URL", raising=False)
    token = _make_token({"sub": "abc-1234", "username": "alice"})
    assert http_auth.auth_headers_for_token(token) == {"X-Auth-Request-User": "alice"}
