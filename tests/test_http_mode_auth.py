"""HTTP-mode auth: id-token identity is relayed to the SurfSense backend.

Contract: when an HTTP request reaches a tool, the validated Cognito token lives
at ``get_access_token()``. ``SurfSenseCognitoProvider`` has embedded the
id_token's ``email`` / ``cognito:username`` (and the raw id_token) under
``claims["upstream_claims"]``. The MCP → SurfSense relay then either:

* HTTP base URL (direct docker network) → inject ``X-Auth-Request-Email`` from
  the id_token email so SurfSense's ``ProxyAuthMiddleware`` provisions the same
  user as the browser login; or
* HTTPS base URL (through Traefik + mPass) → forward the id_token as a Bearer
  for oauth2-proxy to validate and turn into the identity header itself.

When no ``upstream_claims`` are present (older token / native Cognito user) the
relay falls back to the access-token ``username`` claim — exercised by the two
``*_fallback`` cases below.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastmcp.server.auth.auth import AccessToken
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from surfsense_mcp import client as client_module


def _set_request_token(token: AccessToken):
    return auth_context_var.set(AuthenticatedUser(token))


def _reset_request_token(reset_token):
    auth_context_var.reset(reset_token)


@pytest.fixture
def cognito_access_token() -> AccessToken:
    """Token with no ``upstream_claims`` — exercises the native-user fallback."""
    return AccessToken(
        token="cognito-access-token-zzz",
        client_id="mcp-client",
        scopes=["openid"],
        expires_at=int(time.time() + 3600),
        claims={"sub": "abc-1234", "username": "alice"},
    )


@pytest.fixture
def cognito_token_with_id_identity() -> AccessToken:
    """Token carrying id-token identity, as ``SurfSenseCognitoProvider`` builds it.

    The access-token ``username`` is the opaque federated-user UUID; the real
    identity rides in ``upstream_claims`` (email + cognito:username + raw id_token).
    """
    return AccessToken(
        token="cognito-access-token-zzz",
        client_id="mcp-client",
        scopes=["openid"],
        expires_at=int(time.time() + 3600),
        claims={
            "sub": "09daf50c-c0a1-70ec-41e2-443a7270561c",
            "username": "09daf50c-c0a1-70ec-41e2-443a7270561c",
            "upstream_claims": {
                "id_token": "the-id-token-jwt",
                "email": "1020010000020127@askii.ai",
                "cognito:username": "1020010000020127",
            },
        },
    )


def _install_mock_transport(monkeypatch, handler) -> None:
    """Force every httpx.AsyncClient to route through the given handler."""
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args, **kwargs) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


async def test_http_request_injects_email_from_id_token(monkeypatch, cognito_token_with_id_identity):
    """HTTP base URL → direct docker-network path: X-Auth-Request-Email from the
    id_token email (the user the browser login resolves), no Bearer."""
    monkeypatch.setenv("SURFSENSE_BASE_URL", "http://surfsense-backend:8000")
    monkeypatch.delenv("SURFSENSE_JWT", raising=False)

    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json=[{"id": 1, "name": "default"}])

    _install_mock_transport(monkeypatch, handler)

    reset = _set_request_token(cognito_token_with_id_identity)
    try:
        response = await client_module.authed_request("GET", "/api/v1/searchspaces")
    finally:
        _reset_request_token(reset)

    assert response.status_code == 200
    req = recorded[0]
    # The same email the web login keys on — NOT the opaque sub/username UUID.
    assert req.headers["x-auth-request-email"] == "1020010000020127@askii.ai"
    assert "x-auth-request-user" not in {k.lower() for k in req.headers}
    assert "authorization" not in {k.lower() for k in req.headers}


async def test_https_request_forwards_id_token_bearer(monkeypatch, cognito_token_with_id_identity):
    """HTTPS base URL → Traefik+mPass path forwards the *id_token* (carries
    email/cognito:username), not the access token, so oauth2-proxy resolves the
    right user."""
    monkeypatch.setenv("SURFSENSE_BASE_URL", "https://foss-research.local.moneta.dev")
    monkeypatch.delenv("SURFSENSE_JWT", raising=False)

    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json=[])

    _install_mock_transport(monkeypatch, handler)

    reset = _set_request_token(cognito_token_with_id_identity)
    try:
        await client_module.authed_request("GET", "/api/v1/searchspaces")
    finally:
        _reset_request_token(reset)

    req = recorded[0]
    assert req.headers["authorization"] == "Bearer the-id-token-jwt"
    assert "x-auth-request-email" not in {k.lower() for k in req.headers}


async def test_http_request_injects_username_fallback(monkeypatch, cognito_access_token):
    """HTTP base URL, no id-token identity → fall back to access-token username."""
    monkeypatch.setenv("SURFSENSE_BASE_URL", "http://surfsense-backend:8000")
    monkeypatch.delenv("SURFSENSE_JWT", raising=False)
    # Tempt the password fallback path — HTTP mode must not touch it.
    monkeypatch.setenv("SURFSENSE_EMAIL", "ops@example.com")
    monkeypatch.setenv("SURFSENSE_PASSWORD", "shouldnotuse")

    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json=[{"id": 1, "name": "default"}])

    _install_mock_transport(monkeypatch, handler)

    reset = _set_request_token(cognito_access_token)
    try:
        response = await client_module.authed_request("GET", "/api/v1/searchspaces")
    finally:
        _reset_request_token(reset)

    assert response.status_code == 200
    assert len(recorded) == 1
    req = recorded[0]
    assert req.url.path == "/api/v1/searchspaces"
    # ProxyAuthMiddleware reads X-Auth-Request-User and synthesizes the email.
    assert req.headers["x-auth-request-user"] == "alice"
    # The Cognito Bearer is NOT forwarded — SurfSense auth is header-based here.
    assert "authorization" not in {k.lower() for k in req.headers}


async def test_https_request_forwards_access_token_fallback(monkeypatch, cognito_access_token):
    """HTTPS base URL, no id-token identity → fall back to forwarding the raw
    access token. No X-Auth header is pre-injected.

    The MCP server must not pre-inject ``X-Auth-Request-User`` here because
    Traefik's ``strip-auth-headers`` middleware would clear it before mPass
    runs. oauth2-proxy validates the Bearer against the same Cognito JWKS
    FastMCP already used and sets the identity header itself.
    """
    monkeypatch.setenv("SURFSENSE_BASE_URL", "https://foss-research.local.moneta.dev")
    monkeypatch.delenv("SURFSENSE_JWT", raising=False)

    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json=[{"id": 1, "name": "default"}])

    _install_mock_transport(monkeypatch, handler)

    reset = _set_request_token(cognito_access_token)
    try:
        response = await client_module.authed_request("GET", "/api/v1/searchspaces")
    finally:
        _reset_request_token(reset)

    assert response.status_code == 200
    assert len(recorded) == 1
    req = recorded[0]
    assert req.url.path == "/api/v1/searchspaces"
    # The raw Cognito JWT rides as a standard Bearer for oauth2-proxy to validate.
    assert req.headers["authorization"] == "Bearer cognito-access-token-zzz"
    # We must NOT pre-inject the identity header — strip-auth-headers would
    # clear it anyway, but a stale value would mask Bearer validation bugs.
    assert "x-auth-request-user" not in {k.lower() for k in req.headers}


def test_get_header_mcp_wires_aws_cognito_provider(monkeypatch):
    """``get_header_mcp()`` builds the FastMCP HTTP factory with all the
    AWSCognitoProvider knobs we depend on: the redirect path Cognito calls
    back, ``forward_resource=False`` (Cognito User Pools don't honor RFC 8707),
    and the localhost-only DCR redirect allow-list when
    ``MCP_ALLOWED_CLIENT_REDIRECT_URIS`` is empty.

    OIDC discovery happens at provider construction (``OIDCProxy.__init__``
    issues a synchronous ``httpx.get`` to the Cognito ``/.well-known/openid-
    configuration`` URL), so we intercept that call with a stub document
    instead of hitting the real Cognito service.
    """
    import httpx as httpx_module
    from fastmcp.server.auth.providers.aws import AWSCognitoProvider

    from surfsense_mcp import server as server_module

    monkeypatch.setenv("COGNITO_USER_POOL_ID", "ap-southeast-1_TEST123")
    monkeypatch.setenv("COGNITO_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("OIDC_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "test-client-secret")
    monkeypatch.delenv("MCP_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.test.example.com")
    monkeypatch.delenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)

    pool_url = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_TEST123"
    discovery_doc = {
        "issuer": pool_url,
        "authorization_endpoint": f"{pool_url}/oauth2/authorize",
        "token_endpoint": f"{pool_url}/oauth2/token",
        "jwks_uri": f"{pool_url}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }

    def _stub_discovery(url, **kwargs):
        return httpx_module.Response(200, json=discovery_doc, request=httpx_module.Request("GET", url))

    monkeypatch.setattr(httpx_module, "get", _stub_discovery)

    mcp = server_module.get_header_mcp()

    assert isinstance(mcp.auth, AWSCognitoProvider)
    assert mcp.auth._redirect_path == "/auth/callback"
    assert mcp.auth._forward_resource is False
    # Empty MCP_ALLOWED_CLIENT_REDIRECT_URIS → localhost-only defaults
    # (Claude Desktop / Cursor / MCP Inspector all redirect to loopback).
    assert mcp.auth._allowed_client_redirect_uris == [
        "http://localhost:*/*",
        "http://127.0.0.1:*/*",
    ]


def test_get_header_mcp_forwards_client_storage(monkeypatch):
    """``client_storage`` from ``build_oauth_storage()`` reaches AWSCognitoProvider.

    Without forwarding, the provider keeps its default file store and refresh
    tokens vanish on container recreation. We patch the constructor and assert
    on the kwarg rather than wiring up real Valkey here. Also verifies the
    confidential-client wiring: client_secret comes through and
    jwt_signing_key stays None so FastMCP derives it from the secret.
    """
    from fastmcp.server.auth.providers import aws as aws_module

    from surfsense_mcp import server as server_module
    from surfsense_mcp.auth import storage as storage_module

    monkeypatch.setenv("COGNITO_USER_POOL_ID", "ap-southeast-1_TEST123")
    monkeypatch.setenv("COGNITO_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("OIDC_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "test-client-secret")
    monkeypatch.delenv("MCP_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.test.example.com")

    sentinel = object()
    monkeypatch.setattr(storage_module, "build_oauth_storage", lambda: sentinel)

    captured: dict[str, object] = {}

    def fake_init(self, **kwargs):
        captured.update(kwargs)
        # Stop here — we don't need a working provider, just the kwargs.
        raise RuntimeError("stop after constructor capture")

    monkeypatch.setattr(aws_module.AWSCognitoProvider, "__init__", fake_init)

    with pytest.raises(RuntimeError, match="stop after constructor capture"):
        server_module.get_header_mcp()

    assert captured.get("client_storage") is sentinel
    assert captured.get("client_secret") == "test-client-secret"
    assert captured.get("jwt_signing_key") is None


def test_get_header_mcp_supports_public_cognito_client(monkeypatch):
    """Public/PKCE Cognito clients have no secret.

    The server passes ``client_secret=""`` and forwards
    ``MCP_JWT_SIGNING_KEY`` as ``jwt_signing_key``. authlib's
    ``AsyncOAuth2Client`` handles the empty-secret case correctly without
    any token-endpoint-auth-method override — verified end-to-end against
    a real public Cognito client.
    """
    import httpx as httpx_module
    from fastmcp.server.auth.providers.aws import AWSCognitoProvider

    from surfsense_mcp import server as server_module
    from surfsense_mcp.auth import storage as storage_module

    monkeypatch.setenv("COGNITO_USER_POOL_ID", "ap-southeast-1_TEST123")
    monkeypatch.setenv("COGNITO_AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("OIDC_CLIENT_ID", "test-public-client-id")
    monkeypatch.delenv("OIDC_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("MCP_JWT_SIGNING_KEY", "test-signing-key-with-32-bytes-of-entropy")
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.test.example.com")
    monkeypatch.delenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)

    monkeypatch.setattr(storage_module, "build_oauth_storage", lambda: None)

    pool_url = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_TEST123"
    discovery_doc = {
        "issuer": pool_url,
        "authorization_endpoint": f"{pool_url}/oauth2/authorize",
        "token_endpoint": f"{pool_url}/oauth2/token",
        "jwks_uri": f"{pool_url}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }

    def _stub_discovery(url, **kwargs):
        return httpx_module.Response(200, json=discovery_doc, request=httpx_module.Request("GET", url))

    monkeypatch.setattr(httpx_module, "get", _stub_discovery)

    mcp = server_module.get_header_mcp()

    assert isinstance(mcp.auth, AWSCognitoProvider)
    assert mcp.auth.client_id == "test-public-client-id"


async def test_http_request_raises_when_identity_missing(monkeypatch):
    """An AccessToken with no resolvable identity is a hard error, not a silent fallback."""
    monkeypatch.setenv("SURFSENSE_BASE_URL", "http://surfsense-backend:8000")
    monkeypatch.delenv("SURFSENSE_JWT", raising=False)

    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json=[])

    _install_mock_transport(monkeypatch, handler)

    token_without_username = AccessToken(
        token="cognito-access-token-zzz",
        client_id="mcp-client",
        scopes=["openid"],
        expires_at=int(time.time() + 3600),
        claims={"sub": "abc-1234"},
    )
    reset = _set_request_token(token_without_username)
    try:
        with pytest.raises(RuntimeError, match="cannot identify"):
            await client_module.authed_request("GET", "/api/v1/searchspaces")
    finally:
        _reset_request_token(reset)

    # The request must not have been sent at all.
    assert recorded == []
