# CLAUDE.md — surfsense-mcp-server

## What this package is

A [FastMCP v3](https://gofastmcp.com) server that exposes SurfSense as MCP tools (read + write + streaming chat). It is a sibling to `plane-mcp-server/` and follows the same structure.

**No backend changes to SurfSense are allowed** — all tools call existing `surfsense_backend` HTTP routes. Tools may use any HTTP verb the route supports (GET/POST/PUT/DELETE).

## Package layout

```
surfsense-mcp-server/
├── pyproject.toml
├── README.md
├── CLAUDE.md                              ← this file
├── .env.example
├── surfsense_mcp/
│   ├── __init__.py
│   ├── __main__.py                        # CLI entry: ServerMode enum (stdio|http), JSON logging
│   ├── server.py                          # get_stdio_mcp() / get_header_mcp() factories
│   ├── client.py                          # httpx wiring + retry; no auth (see auth/)
│   ├── auth/
│   │   ├── __init__.py                    # build_auth_headers() dispatcher + retry-gate
│   │   ├── stdio.py                       # SURFSENSE_JWT / password fallback / cache
│   │   ├── http.py                        # AccessToken → X-Auth-Request-Email / id_token Bearer
│   │   ├── cognito.py                     # SurfSenseCognitoProvider: id_token claims → upstream_claims
│   │   └── storage.py                     # MCP_OAUTH_STORAGE_URL → ValkeyStore + Fernet
│   └── tools/
│       ├── __init__.py                    # register_tools(mcp) — calls all per-module register fns
│       ├── search_spaces.py               # list / get / create / update / delete
│       ├── documents.py                   # list / search / get / upload / update / delete / status / type_counts
│       ├── threads.py                     # list / get / delete / history + query (SSE streaming)
│       ├── reports.py                     # list / get / export / delete
│       ├── notes.py                       # create
│       └── logs.py                        # get
└── tests/
    ├── conftest.py                        # mock_transport fixture, FAKE_JWT, json_response
    └── test_tools.py                      # coverage for multiple tool surfaces and auth behavior
```

## Dev commands

```bash
# Install
cd surfsense-mcp-server
uv venv && uv pip install -e ".[dev]"

# Tests (no live backend needed — httpx is mocked)
pytest

# Lint / format
ruff check surfsense_mcp/
ruff format surfsense_mcp/

# Upgrade fastmcp to latest v3
uv sync --extra dev --upgrade-package fastmcp

# Run stdio locally (JWT paste)
SURFSENSE_BASE_URL=http://localhost:8000 SURFSENSE_JWT=<jwt> python -m surfsense_mcp stdio

# Run stdio with password fallback (no JWT paste)
SURFSENSE_BASE_URL=http://localhost:8000 \
SURFSENSE_EMAIL=admin@example.com SURFSENSE_PASSWORD=<pw> \
python -m surfsense_mcp stdio

# Run HTTP mode (multi-user, auto-OAuth via AWSCognitoProvider)
# Two valid base-URL shapes; the MCP server auto-detects from the scheme:
#   • https://foss-research.local.moneta.dev → goes through Traefik+mPass;
#     forward the Cognito id_token; oauth2-proxy validates and sets X-Auth-Request-Email.
#   • http://surfsense-backend:8000 → direct on docker network; inject
#     X-Auth-Request-Email from the id_token's email claim.
# Confidential Cognito client (has a secret):
SURFSENSE_BASE_URL=http://surfsense-backend:8000 \
MCP_BASE_URL=https://foss-research-mcp.local.moneta.dev \
COGNITO_USER_POOL_ID=ap-southeast-1_XXXXX \
COGNITO_AWS_REGION=ap-southeast-1 \
OIDC_CLIENT_ID=...        \
OIDC_CLIENT_SECRET=...    \
python -m surfsense_mcp http   # binds :8211

# Public/PKCE Cognito client (no secret) — supply MCP_JWT_SIGNING_KEY instead:
SURFSENSE_BASE_URL=http://surfsense-backend:8000 \
MCP_BASE_URL=https://foss-research-mcp.local.moneta.dev \
COGNITO_USER_POOL_ID=ap-southeast-1_XXXXX \
COGNITO_AWS_REGION=ap-southeast-1 \
OIDC_CLIENT_ID=...                          \
MCP_JWT_SIGNING_KEY=$(openssl rand -hex 32) \
python -m surfsense_mcp http
```

In the Moneta devstack everything except `OIDC_CLIENT_ID/SECRET` and
`OIDC_BASE_URI` is auto-derived (`make dev.setup`). One manual step in
the AWS Console: add `${MCP_BASE_URL}/auth/callback` to the Cognito app
client's allowed callback URLs (no new client required).

## Architecture

### Transport modes

- **stdio** — single-user, per-developer install (Claude Desktop / Cursor / VS Code on a laptop). `SURFSENSE_JWT` env var carries the token; password fallback (`SURFSENSE_EMAIL` + `SURFSENSE_PASSWORD`) is available for CI / long-running sessions. `get_stdio_mcp()` builds a FastMCP instance without an auth provider; the JWT is read directly in `client.py` and sent as `Authorization: Bearer`.
- **http** — multi-user, hosted. `get_header_mcp()` attaches FastMCP's `AWSCognitoProvider`, which makes the service itself a full OAuth 2.0 authorization server: it publishes `/.well-known/oauth-authorization-server` + `/.well-known/oauth-protected-resource` (RFC 8414 / 9728), implements the `/register` DCR shim over the pre-registered Cognito app client (RFC 7591), and proxies `/authorize` / `/auth/callback` / `/token` to Cognito. MCP clients (Claude Desktop / Cursor) discover all of this and run the OAuth flow automatically — no manual Bearer paste. Port: `8211`. This service is **not** behind mPass; FastMCP is the sole auth layer for `/mcp`. The MCP → SurfSense call has two supported shapes, picked from `SURFSENSE_BASE_URL`'s scheme: HTTPS forwards the Cognito **id_token** through Traefik+mPass (oauth2-proxy validates against the Cognito JWKS and sets `X-Auth-Request-Email`), while HTTP goes direct on the docker network with the MCP server injecting `X-Auth-Request-Email` from the id_token's `email` claim. The id_token (not the access token) is used because a Cognito access token carries no `email` claim and, for federated users, an opaque UUID `username` — forwarding it resolves the wrong SurfSense account. `SurfSenseCognitoProvider` (see `auth/cognito.py`) captures the id_token's claims at token-exchange time. Either way SurfSense's `ProxyAuthMiddleware` reads the header and auto-provisions the user. Public Cognito clients (no `client_secret`) are supported by setting `MCP_JWT_SIGNING_KEY` instead — the server passes `client_secret=""` and the explicit signing key to `AWSCognitoProvider`. authlib's `AsyncOAuth2Client` handles the empty-secret token exchange correctly without any further override.

### Auth model

SurfSense issues short-lived JWTs from `fastapi-users` (no API-key concept). The MCP server talks to SurfSense with a fastapi-users JWT (stdio), a forwarded Cognito Bearer (HTTP + HTTPS base URL), or an injected identity header (HTTP + HTTP base URL).

- **stdio (primary):** JWT from `SURFSENSE_JWT` env var — user pastes a fresh one when it expires. No refresh.
- **stdio (optional fallback):** if `SURFSENSE_JWT` is unset and `SURFSENSE_EMAIL` + `SURFSENSE_PASSWORD` are set, the client calls `POST /auth/jwt/login`, caches the token for `TOKEN_TTL` seconds (default 3300 = 55 min), and auto-re-authenticates once on 401. Intended for CI / long-running stdio sessions.
- **http (HTTPS base URL — through mPass):** `SurfSenseCognitoProvider` (subclass of `AWSCognitoProvider`) validates the inbound Cognito access token against the pool's JWKS, and additionally decodes the upstream **id_token** at token-exchange time (`_extract_upstream_claims`), stashing its `email` / `cognito:username` / raw id_token under `claims["upstream_claims"]`. `auth/http.py:bearer_header()` forwards that **id_token** as `Authorization: Bearer <…>`; oauth2-proxy validates it against the same JWKS, sets `X-Auth-Request-Email` from the `email` claim, and forwards. (The access token can't be used here: it has no `email` and a UUID `username` for federated users, so oauth2-proxy would fall back to `sub` and provision the wrong account.) Requires `OAUTH2_PROXY_SKIP_JWT_BEARER_TOKENS=true` and `OAUTH2_PROXY_OIDC_AUDIENCE_CLAIMS=aud,client_id` on oauth2-proxy — the id_token carries the audience as `aud` (matching the cookie flow), so `aud` must be accepted.
- **http (HTTP base URL — direct on docker network):** `auth/http.py:identity_header()` forwards the id_token's `email` claim as `X-Auth-Request-Email` (falling back to `cognito:username`, then the access-token `username`, as `X-Auth-Request-User`). The Cognito Bearer is **not** forwarded; trust is the docker network boundary. Strictly faster (no Traefik hop, no JWT re-validation) and the right choice when MCP and SurfSense share a network.

`auth/__init__.py:build_auth_headers()` is the dispatcher: if an HTTP request token is in scope (`auth/http.py:request_token()` returns non-None), it delegates to `auth/http.py:auth_headers_for_token()`, which picks `bearer_header` for an HTTPS `SURFSENSE_BASE_URL` and `identity_header` for HTTP. Otherwise it returns `{Authorization: Bearer <surfsense-jwt>}` from `auth/stdio.py:resolve_jwt()`. The 401-retry-once path (gated by `auth_came_from_password()`) fires only in stdio password mode — HTTP-mode 401s are surfaced unchanged because they indicate a real provisioning failure, not a stale token.

### Connecting Claude Desktop / Cursor (HTTP mode)

Add a "Custom connector" in Claude Desktop / Cursor pointing at
`https://<host>/mcp`. The client will:

1. Hit `/mcp`, receive `401 WWW-Authenticate: Bearer resource_metadata="…"`.
2. Fetch `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server` — both published by `AWSCognitoProvider` automatically.
3. `POST /register` — FastMCP's DCR shim returns a client registration backed by the single pre-registered Cognito app client (no new Cognito client per MCP consumer; Cognito has no DCR of its own).
4. Open a browser to `/authorize` (PKCE) which redirects to Cognito. User logs in. Cognito redirects back to `/auth/callback`.
5. Exchange the code for tokens at `/token`. The Cognito access token is returned to the MCP client, which sends it as `Authorization: Bearer` on every `/mcp` call.

No manual Bearer paste and no Cognito client config needed on the MCP client side — the DCR shim handles it.

**One-time AWS Console prerequisite:** on the existing Cognito app client (`OIDC_CLIENT_ID`), add `${MCP_BASE_URL}/auth/callback` to the allowed callback URLs and ensure `authorization_code` is in the enabled grant types. No new app client, no new secret.

**MCP Inspector for local dev:** `npx @modelcontextprotocol/inspector`, transport "Streamable HTTP", URL `https://foss-research-mcp.local.moneta.dev/mcp` — Inspector follows the same discovery + OAuth flow.

### HTTP-mode storage

`AWSCognitoProvider` (via `OAuthProxy`) keeps six collections of OAuth state — DCR client registrations, in-flight authorize transactions, authorization codes, upstream Cognito access/refresh tokens, JTI mappings, and refresh-token metadata. Two backends:

- **Default — encrypted file tree** under `~/.local/share/fastmcp/oauth-proxy/<fingerprint>/` inside the container. Survives `docker compose restart`, but `docker compose down && up` recreates the container's writable layer and wipes everything → every MCP client re-OAuths on next call. Single-replica only.
- **Production — Valkey/Redis** (`MCP_OAUTH_STORAGE_URL=redis://valkey:6379/<db>`). `auth/storage.py:build_oauth_storage()` parses the URL, constructs a `ValkeyStore`, and wraps it in `FernetEncryptionWrapper` keyed off `OIDC_CLIENT_SECRET` (preferred — confidential clients) or `MCP_JWT_SIGNING_KEY` (fallback — public/PKCE clients). The same HKDF derivation FastMCP uses for the file store, so on-disk RDB never carries plaintext. State survives container recreation; multi-replica works as long as all replicas point at the same instance.

In the Moneta devstack the compose file always sets `MCP_OAUTH_STORAGE_URL=redis://valkey:6379/11` so the backend is Valkey by default. `__main__.py:warn_if_storage_missing_in_production()` logs a warning when `MCP_ENV=production` and the URL is unset; we don't hard-fail because evaluation/local runs of the image should still come up.

**Eviction caveat.** Devstack Valkey runs `--maxmemory 512mb --maxmemory-policy allkeys-lru` (server-wide). Under memory pressure refresh tokens *can* be evicted regardless of TTL. At current load this is theoretical, but for high-traffic prod either bump `--maxmemory` or switch the policy to `volatile-lru`. Per-DB eviction policies are not a feature of Valkey/Redis; the change affects every consumer on the instance.

**Key shape.** Stored keys appear as `<collection>::<key>` (e.g. `mcp-authorization-codes::1tvOoh8...`). The `::` is `py-key-value-aio`'s `DEFAULT_COMPOUND_SEPARATOR` — single `:` was avoided because Redis-shaped data routinely uses it for in-key namespacing (`session:abc`, `user:42:profile`).

### Tool conventions

- Every tool is decorated with `@mcp.tool()` and registered via a `register_*` function called from `tools/__init__.py:register_tools()`.
- Tools return raw `dict` / `list` — no Pydantic re-modeling of SurfSense's response schemas.
- Tools `raise` on non-2xx responses so FastMCP surfaces errors to the MCP client.
- All tools open and close `httpx.AsyncClient` within the call using an `async with` block (client is not shared across calls).
- Write tools (POST/PUT/DELETE) are allowed. Follow SurfSense's existing request schemas — do **not** introduce new fields.
- Chat/query tools consume SSE from `POST /api/v1/new_chat` via `httpx.AsyncClient.stream(...)`. Parse `text-delta` events (and the documented control events: `start`, `start-step`, `finish`, `finish-step`, `text-start`, `text-end`, `data-thinking-step`, `data-thread-title-update`) into a single concatenated string; fall back to raw JSON / raw text when the event shape is unknown. See DocuMentor's `_query_surfsense` for the reference event taxonomy.
- Tools that create a thread on demand (when `thread_id` is `None`) must `POST /api/v1/threads` first, then stream, and return the new `thread_id` so callers can continue the conversation.
- Binary/export responses (e.g. `GET /api/v1/reports/{id}/export`) return content-type + size, not inline bytes.

### FastMCP version

Requires **fastmcp >= 3.0.0, < 4.0.0**. The v3 HTTP mode API differs from v2:

```python
# v3 — correct
header_app = header_mcp.http_app(middleware=cors, stateless_http=True)
app = Starlette(routes=[Mount("/", app=header_app)], lifespan=header_app.lifespan)

# v2 — do NOT use
app = header_mcp.http_app(middleware=cors)  # different signature
```

`lifespan` must be `header_app.lifespan` (not a lambda wrapping it).

## Key constraints

- **No backend changes** — all tools must call routes that already exist in `surfsense_backend`. Verify in `surfsense_backend/app/routes/` before adding any tool. If a route is missing, drop the tool — do not add one to the backend.
- **No new MCP resources** — tools only.
- **No Pydantic re-modeling** — return raw dicts from httpx responses. SurfSense's schemas are not imported here.
- **No token refresh in JWT-paste mode** — the server does not attempt to refresh `SURFSENSE_JWT`. Expiry surfaces to the MCP client as a 401. Password-fallback mode (stdio) is the only path with auto-reauth; HTTP mode relies on the MCP client (Claude Desktop / Cursor) to refresh its Cognito token via the OAuth refresh flow.
- **HTTP mode never uses password login** — only stdio is allowed to log in with email/password. HTTP requests must arrive with a Cognito-issued Bearer JWT validated by `AWSCognitoProvider`.
- **HTTP mode picks the relay strategy from `SURFSENSE_BASE_URL`'s scheme.** HTTPS → forward the Cognito **id_token** through Traefik+mPass (oauth2-proxy is the verifier). HTTP → inject `X-Auth-Request-Email` (from the id_token's `email` claim) directly on the trusted docker network (no Bearer forwarded; trust = network boundary). Both rely on `SurfSenseCognitoProvider` having captured the id_token's identity — never the access token's `username`/`sub`, which misidentify federated users. The dispatch lives in `auth/http.py:auth_headers_for_token`; do not bypass it by reading the env var elsewhere.
- **TLS trust for non-public CAs.** When `SURFSENSE_BASE_URL` is HTTPS against a host with a private CA (e.g. Traefik mkcert at `*.local.moneta.dev`), set `SURFSENSE_CA_BUNDLE_PATH` to the cert path. `client._ssl_verify()` layers it on top of the system trust store; left unset, httpx's default verify behavior applies.

## Binary uploads & MCP transport limits

Two upload tools live in `surfsense_mcp/tools/documents.py`:

- `upload_document(file_path, ...)` — for stdio or any context where the MCP server can read the file off disk. Preferred when available; no encoding overhead.
- `upload_document_content(filename, content_base64, ...)` — for chat attachments and HTTP-mode callers without disk access. Bytes ride as base64 inside the JSON-RPC `tools/call` arguments.

Both enforce a 500 MB per-file ceiling that mirrors `surfsense_backend/app/routes/documents_routes.py:53` (`MAX_FILE_SIZE_BYTES`).

**Why base64 at the MCP boundary, not multipart.** MCP is JSON-RPC 2.0 — every tool argument must be JSON-serializable. FastMCP v3 has no input-side binary type: `fastmcp/utilities/types.py:238-276` ships only output helpers (`Image` / `Audio` / `File`), and `fastmcp/tools/function_parsing.py:43-50` actively replaces `bytes` in tool input schemas with `_UnserializableType`. Base64 inside a JSON string is the only available channel for inline bytes.

**Why we don't add a sidecar `POST /upload` route on the HTTP app.** Mechanically easy — drop a `Route("/upload", ...)` next to `/healthz` in `surfsense_mcp/__main__.py:117-148` (~80 LOC + a JWKS-validating dependency, since FastMCP's `get_access_token()` only resolves inside a tool-call scope and can't be reused from a Starlette route). But it does not help the primary use case. Claude Desktop / Cursor / Windsurf only emit `tools/call`; they will not autonomously `PUT` chat-attached bytes to a non-MCP URL — tool *results* are returned to the model as text/structured content, not as instructions the host acts on. The endpoint would only benefit programmatic / curl-style callers, which didn't justify the duplicate auth surface or a second supported transport for uploads.

**What would change this decision.** Two concrete signals to watch: (a) the MCP spec adds a binary input frame or sanctions out-of-band upload triggers in tool results; (b) Claude Desktop / Cursor ship a feature that lets a tool result instruct the host to upload a referenced attachment to a URL. Until then, base64 stays.

**Do-not-change pointers.** Do not add a `Route("/upload", ...)` in `__main__.py` unless one of the signals above lands. Do not try to switch the tool inputs to a `bytes`-typed parameter — `function_parsing.py:43-50` will reject it. The existing 500 MB guard in `surfsense_mcp/tools/documents.py` is the correct upper bound and matches the backend; lowering it requires a backend-side change to match.

## Relevant SurfSense backend files

When adding tools, check these backend files to confirm route paths and query parameters:

| Backend file | What it defines |
|---|---|
| `surfsense_backend/app/routes/search_space_routes.py` | `/api/v1/searchspaces` (list/get/create/update/delete) — list supports `owned_only`, `skip`, `limit` |
| `surfsense_backend/app/routes/documents_routes.py` | `/api/v1/documents`, `/documents/search`, `/documents/{id}` (GET/PUT/DELETE), `/documents/fileupload`, `/documents/status`, `/documents/type-counts` |
| `surfsense_backend/app/routes/threads_routes.py` | `/api/v1/threads`, `/threads/{id}`, `/threads/{id}/messages`, `POST /api/v1/new_chat` (SSE stream) |
| `surfsense_backend/app/routes/reports_routes.py` | `/api/v1/reports`, `/reports/{id}/content`, `/reports/{id}/export` |
| `surfsense_backend/app/routes/logs_routes.py` | `/api/v1/logs` |
| `surfsense_backend/app/routes/notes_routes.py` | `POST /api/v1/search-spaces/{id}/notes` |
| `surfsense_backend/app/routes/auth_routes.py` | `/users/me`, `POST /auth/jwt/login` |

> `sort_column_map` in `documents_routes.py` only accepts `"created_at"`, `"title"`, `"document_type"` — `"updated_at"` is not a valid sort key.
>
> Confirm each route's existence and exact path before implementing a tool — DocuMentor (the reference port source) targets a different SurfSense fork and some paths may not match this fork. If a route is missing, drop the tool (no backend additions).

## Test fixtures

`tests/conftest.py` provides:

- `mock_transport` — patches `httpx.AsyncClient.__init__` with a `MockTransport`. Returns a `setup(handler)` callable; calling it registers a response handler and returns a `recorded: list[httpx.Request]` for assertion.
- `_env` (autouse) — sets `SURFSENSE_BASE_URL` and `SURFSENSE_JWT` per test via `monkeypatch`.
- `json_response(payload, status_code=200)` — helper to build `httpx.Response` from a dict.

Tests use `Client(get_stdio_mcp())` (FastMCP in-process client) — no subprocess, no network.

## SurfSense backend port

The default SurfSense backend port is `8000` (`UVICORN_PORT`). Instances vary — confirm with the operator before hardcoding. In the Moneta devstack it may run on a different port (e.g. `8929`).
