# SurfSense MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes a SurfSense knowledge base to any MCP-compatible client (Claude Desktop, Cursor, MCP Inspector, VS Code, Windsurf, …).

The server is a [FastMCP v3](https://gofastmcp.com) wrapper over SurfSense's existing HTTP routes — read, write, and streaming chat — with **no backend changes** required.

## Tools

Read:

| Tool | Description |
|---|---|
| `list_search_spaces` / `get_search_space` | List / fetch search spaces the user can access. |
| `search_documents` | Title ILIKE search on documents within a search space. |
| `get_document` / `get_recent_documents` / `list_documents` | Fetch documents by id / list recent / list all. |
| `get_document_status` / `get_document_type_counts` | Per-space ingestion counters. |
| `list_research_threads` / `get_research_thread` / `get_thread_messages` | Browse chat/research threads. |
| `list_reports` / `get_report` / `get_report_content` / `export_report` | Browse and export research reports. |
| `get_logs` | Fetch backend log entries (operator tool). |

Write:

| Tool | Description |
|---|---|
| `create_search_space` / `update_search_space` / `delete_search_space` | Manage spaces. |
| `upload_document` | Upload a file from disk (multipart, no base64 inflation). |
| `upload_document_content` | Upload bytes inline as base64 (chat attachments / no-disk callers). |
| `update_document` / `delete_document` | Modify or remove a document. |
| `delete_research_thread` / `delete_report` | Remove threads or reports. |
| `create_note` / `delete_note` | Add or remove a note in a search space. |

Chat (streaming):

| Tool | Description |
|---|---|
| `query_surfsense` | Ask SurfSense a natural-language question. Streams over SSE from `POST /api/v1/new_chat`, creates a thread on demand, returns the concatenated answer + thread id. Subsumes summarize / compare / extract / quick_research / deep_research — just ask. |

## Transport modes

The server supports two transports. Pick the one that matches how you're running it.

- **stdio** — single-user, runs as a subprocess of your MCP client (Claude Desktop, Cursor, VS Code, …) on your laptop. Auth is a SurfSense JWT pasted into config.
- **http** — multi-user, hosted on a public/internal URL. Auth is OAuth 2.0 — clients discover the auth server, register themselves (DCR shim), and run a browser login flow. No manual Bearer paste.

If you're an end user setting up Claude Desktop or Cursor against your local SurfSense, you want stdio. If you're an operator deploying the MCP server inside the Moneta `foss-server-bundle-devstack` (or any multi-user setup), you want http.

## How auth works

### stdio

The MCP client launches the server as a subprocess and forwards requests over stdio. The server reads `SURFSENSE_JWT` (or `SURFSENSE_EMAIL` + `SURFSENSE_PASSWORD` for the password fallback) from its environment and sends `Authorization: Bearer <surfsense-jwt>` to the SurfSense backend on every call. JWTs are short-lived; when one expires, paste a fresh one. Password fallback auto-re-authenticates once on 401 (intended for CI / long-running sessions).

### http

This is the more interesting layer. FastMCP's [`AWSCognitoProvider`](https://gofastmcp.com/integrations/aws-cognito) makes the MCP server itself a full OAuth 2.0 authorization server:

1. **Discovery** — the server publishes `/.well-known/oauth-authorization-server` (RFC 8414) and `/.well-known/oauth-protected-resource` (RFC 9728). MCP clients hit `/mcp`, get a `401 WWW-Authenticate: Bearer resource_metadata=…` challenge, and fetch the metadata.
2. **DCR shim** — Cognito has no native [Dynamic Client Registration](https://datatracker.ietf.org/doc/html/rfc7591). The server's `/register` endpoint accepts MCP-client registrations and returns the *single, pre-registered* Cognito app client to all of them. So Claude Desktop, Cursor, MCP Inspector — every client looks like its own OAuth client to itself, but they all share the underlying Cognito client.
3. **Authorization code flow with PKCE** — the server proxies `/authorize` → Cognito's hosted UI, captures the code at `/auth/callback`, exchanges it server-side at Cognito's `/oauth2/token` (with the confidential client secret, or PKCE-only when the Cognito client is public — `client_secret=""` is passed through and authlib handles the exchange without a Basic header), and returns the Cognito access token to the MCP client.
4. **Bearer validation** — every `/mcp/*` request is Bearer-validated against the Cognito user pool's JWKS (no live calls to Cognito on the hot path). `SurfSenseCognitoProvider` also decodes the upstream **id_token** at token-exchange time and stashes its `email` / `cognito:username` under the FastMCP token's `upstream_claims`.
5. **Backend identity injection** — how identity reaches SurfSense depends on `SURFSENSE_BASE_URL`'s scheme. On the **HTTP** (direct docker-network) shape, tools forward the id_token's `email` as `X-Auth-Request-Email`; SurfSense's `ProxyAuthMiddleware` reads it and auto-provisions/loads the user (the Cognito Bearer is never forwarded). On the **HTTPS** (through Traefik+mPass) shape, the id_token is forwarded as the Bearer and oauth2-proxy sets the identity header itself. Crucially it's the **id_token**, not the access token: a Cognito access token has no `email` claim and an opaque UUID `username` for federated users, so it would resolve a *different* SurfSense account than the web login.

This means the MCP server is the only Moneta service that does *not* sit behind mPass (oauth2-proxy ForwardAuth) — MCP clients can't follow interactive OIDC redirects mid-stream, so we let FastMCP handle the full OAuth dance instead. The MCP → SurfSense leg also bypasses mPass: it goes direct on the docker network, with the trust boundary being the network itself (no host port published).

**Confidential vs. public Cognito clients.** Both work. If the Cognito app client has a secret, set `OIDC_CLIENT_SECRET` and FastMCP derives everything else from it. If the client is public (PKCE, no secret), leave `OIDC_CLIENT_SECRET` unset and provide `MCP_JWT_SIGNING_KEY` instead — that key signs FastMCP-issued JWTs and seeds the Fernet wrapper around the OAuth-state store. Generate it once with `openssl rand -hex 32` and treat it like any other long-lived signing secret (rotating it invalidates all persisted OAuth state, so every MCP client will re-OAuth on next call).

**One-time AWS Console prereq.** On the existing Cognito app client (`OIDC_CLIENT_ID`), add `${MCP_BASE_URL}/auth/callback` to the allowed callback URLs. No new Cognito client, no new secret.

For more depth (file paths, FastMCP version constraints, transport edge cases), see `CLAUDE.md`.

## Install

This package is **not published to PyPI** — install it directly from the source tree.

```bash
cd surfsense-mcp-server
uv venv && uv pip install -e ".[dev]"
```

Or with pip:

```bash
cd surfsense-mcp-server
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Run — stdio

```bash
SURFSENSE_BASE_URL=http://localhost:8000 \
SURFSENSE_JWT=<paste-jwt-here> \
python -m surfsense_mcp stdio
```

### Get a JWT (stdio only)

**Option A — browser localStorage (easiest)**

1. Log in to your SurfSense instance.
2. Open DevTools → Console and run:
   ```js
   localStorage.getItem('surfsense_bearer_token')
   ```
3. Copy the printed value into `SURFSENSE_JWT`.

**Option B — Network tab.** Open DevTools → Network, filter by your SurfSense backend host, click any API request, and copy the `Authorization: Bearer …` header value (strip the `Bearer ` prefix).

**Option C — curl.**

```bash
curl -X POST https://<surfsense-host>/auth/jwt/login \
  -d 'username=<email>&password=<password>'
# {"access_token":"<jwt>","token_type":"bearer"}
```

> The token is short-lived (default ~1 day). Re-paste when tools start returning `401`, or use the password fallback below.

### Password fallback (CI / long-running stdio)

```bash
SURFSENSE_BASE_URL=http://localhost:8000 \
SURFSENSE_EMAIL=admin@example.com \
SURFSENSE_PASSWORD=<password> \
python -m surfsense_mcp stdio
```

The server calls `POST /auth/jwt/login`, caches the token for `TOKEN_TTL` seconds (default 3300 = 55 min), and auto-re-authenticates once on 401. **HTTP mode never uses this path** — only stdio.

## Run — http

In the Moneta devstack the MCP server runs as a docker-compose service and everything is auto-wired by `make dev.up.surfsense`. To run it standalone:

```bash
# Confidential Cognito client (has a secret)
SURFSENSE_BASE_URL=http://localhost:8000 \
MCP_BASE_URL=https://my-mcp.example.com \
COGNITO_USER_POOL_ID=ap-southeast-1_XXXXXXXXX \
COGNITO_AWS_REGION=ap-southeast-1 \
OIDC_CLIENT_ID=<cognito-app-client-id> \
OIDC_CLIENT_SECRET=<cognito-app-client-secret> \
python -m surfsense_mcp http   # binds 0.0.0.0:8211

# Public/PKCE Cognito client (no secret)
SURFSENSE_BASE_URL=http://localhost:8000 \
MCP_BASE_URL=https://my-mcp.example.com \
COGNITO_USER_POOL_ID=ap-southeast-1_XXXXXXXXX \
COGNITO_AWS_REGION=ap-southeast-1 \
OIDC_CLIENT_ID=<cognito-public-client-id> \
MCP_JWT_SIGNING_KEY=$(openssl rand -hex 32) \
python -m surfsense_mcp http
```

The server validates the four base env vars on startup (see `surfsense_mcp/__main__.py:REQUIRED_HTTP_ENV_VARS`) and additionally requires exactly one of `OIDC_CLIENT_SECRET` (confidential) or `MCP_JWT_SIGNING_KEY` (public). It refuses to start otherwise.

## MCP client config

### Claude Desktop / Cursor (stdio — local install)

Locate `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`) and add:

```json
{
  "mcpServers": {
    "surfsense": {
      "command": "/absolute/path/to/surfsense-mcp-server/.venv/bin/python",
      "args": ["-m", "surfsense_mcp", "stdio"],
      "env": {
        "SURFSENSE_BASE_URL": "http://localhost:8000",
        "SURFSENSE_JWT": "<paste-jwt-here>"
      }
    }
  }
}
```

> **Why not `uvx`?** The package is not on PyPI so `uvx surfsense-mcp-server` won't resolve. Use the venv Python path shown above.

### Claude Desktop / Cursor (http — remote, auto-OAuth)

Settings → Connectors → "Add custom connector" — paste your MCP URL:

```
https://<prefix>research-mcp.<platform-domain>/mcp
```

That's all. The client follows OAuth discovery, opens a browser tab to Cognito for sign-in, and stores tokens in the OS keychain. No Bearer paste, no headers config.

For older clients that don't support OAuth discovery yet, the [`mcp-remote`](https://github.com/geelen/mcp-remote) shim still works — but the native config above is the recommended path.

## Testing locally with MCP Inspector

The fastest way to verify a fresh deploy. Inspector is `npx`-installed, runs locally, and shows every step of the OAuth handshake.

1. **Bring up the devstack** (or just the SurfSense + MCP services):

   ```bash
   cd /path/to/foss-server-bundle-devstack
   make dev.up.surfsense        # starts surfsense-backend + surfsense-mcp + deps
   ```

2. **Trust the devstack's self-signed cert** so Inspector can talk to it. Either install `traefik/certs/local.crt` system-wide, or point Node at it explicitly:

   ```bash
   export NODE_EXTRA_CA_CERTS=/path/to/foss-server-bundle-devstack/traefik/certs/local.crt
   ```

3. **Launch Inspector**:

   ```bash
   npx @modelcontextprotocol/inspector
   ```

4. **Connect**:
   - Transport: **Streamable HTTP**
   - URL: `https://foss-research-mcp.local.moneta.dev/mcp`
   - Click the standard "Connect" / "Quick Auth Flow" — *not* the "Guided" / debug variant. The debug flow shows a copy-the-code step that's only useful for diagnostics; the quick flow runs the OAuth round-trip automatically.

5. Inspector opens a browser tab → you log in via Cognito → tab closes → Inspector shows the `tools/list`. Call `list_search_spaces` to verify everything works end-to-end. The user is auto-provisioned on the SurfSense backend on first call (via `X-Auth-Request-User`).

## Common errors

- **`Token exchange with identity provider failed: invalid_grant: `** (empty error_description, in MCP server logs at the `/auth/callback` step). Two things to check, in order:
  1. The Cognito app client's "Allowed callback URLs" must contain *exactly* `${MCP_BASE_URL}/auth/callback` — no trailing slash, lowercase, identical scheme/host.
  2. `forward_resource=False` must be set on `AWSCognitoProvider` (`surfsense_mcp/server.py`). Cognito User Pools don't honor RFC 8707 Resource Indicators; if `forward_resource` is left at the default `True`, Cognito gets a `resource` param at `/authorize` that it can't reconcile at `/token`, and rejects the grant.

- **`UNABLE_TO_VERIFY_LEAF_SIGNATURE` / `DEPTH_ZERO_SELF_SIGNED_CERT`** in MCP Inspector or other Node clients. The devstack uses a self-signed cert. Either install it system-wide or set `NODE_EXTRA_CA_CERTS=/path/to/foss-server-bundle-devstack/traefik/certs/local.crt` before launching the client.

- **`401 Unauthorized` from `/mcp` after successful OAuth.** Almost always means the inbound Cognito Bearer failed JWKS validation — check the user pool / region / client-id config and that `SurfSenseCognitoProvider` (subclass of `AWSCognitoProvider`) is in use.

- **A *different* SurfSense user is created/loaded than your web login** (e.g. a UUID-named account like `09daf50c-…@askii.ai` instead of `1020010000020127@askii.ai`). The relay is identifying you by the Cognito **access token** (no `email`, opaque `username`) instead of the **id_token**. Confirm `get_header_mcp()` builds `SurfSenseCognitoProvider` (not the stock `AWSCognitoProvider`) so `_extract_upstream_claims` captures the id_token, and that the Cognito client returns an id_token (the `openid` scope is requested). Logs will show a `No upstream id_token` warning when this is misconfigured.

- **`Cognito username claim missing on validated token`** in MCP server logs. Same root cause as above; the server raises here rather than silently sending a header with no value.

## Configuration

| Variable | Required | Mode | Purpose |
|---|---|---|---|
| `SURFSENSE_BASE_URL` | yes | both | Base URL of the SurfSense backend, no trailing slash. In docker the devstack sets this to `http://surfsense-backend:8000`. |
| `SURFSENSE_JWT` | yes | stdio | JWT forwarded as `Authorization: Bearer`. |
| `SURFSENSE_EMAIL` / `SURFSENSE_PASSWORD` | optional | stdio | Password fallback when `SURFSENSE_JWT` is unset. |
| `TOKEN_TTL` | optional | stdio | Cache lifetime for password-fallback tokens (seconds, min 60, default 3300). |
| `MCP_BASE_URL` | yes | http | Public URL where this MCP server is reachable, e.g. `https://foss-research-mcp.local.moneta.dev`. Used to build the OAuth callback URL. |
| `COGNITO_USER_POOL_ID` / `COGNITO_AWS_REGION` | yes | http | Cognito pool to validate Bearer tokens against. |
| `OIDC_CLIENT_ID` | yes | http | Cognito app client used for the OAuth proxy. Reuses the existing oauth2-proxy client in the Moneta devstack. |
| `OIDC_CLIENT_SECRET` | conditional | http | Set when the Cognito client is confidential. Leave unset for public/PKCE clients (then `MCP_JWT_SIGNING_KEY` is required). Exactly one of the two must be set. |
| `MCP_JWT_SIGNING_KEY` | conditional | http | Required when the Cognito client is public (no secret). High-entropy string used both to sign FastMCP-issued JWTs and to derive the Fernet key for `MCP_OAUTH_STORAGE_URL`. Generate with `openssl rand -hex 32`. Rotating it invalidates persisted OAuth state. |
| `MCP_ALLOWED_CLIENT_REDIRECT_URIS` | optional | http | Comma-separated allow-list for DCR-registered redirect URIs. Empty/unset → localhost-only defaults (covers Claude Desktop, Cursor, MCP Inspector). Add server-side MCP clients explicitly. |
| `MCP_OAUTH_STORAGE_URL` | optional | http | Valkey/Redis URL for OAuth state (`redis://valkey:6379/11`). Unset → encrypted file store on the container filesystem; tokens are wiped on container recreation and there's no path to multi-replica. Set this in any deployment that needs to survive `docker compose down && up` or scale beyond one replica. State at rest is Fernet-encrypted using a key derived from `OIDC_CLIENT_SECRET` (preferred — confidential clients) or `MCP_JWT_SIGNING_KEY` (fallback — public/PKCE clients). |
| `MCP_ENV` | optional | http | `production` triggers warnings when `MCP_ALLOWED_ORIGINS` is unset/`*` or `MCP_OAUTH_STORAGE_URL` is unset. Default `development`. |
| `MCP_ALLOWED_ORIGINS` | optional | http | Comma-separated CORS origins. Default `*`. |
| `MCP_LOG_LEVEL` | optional | both | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` (case-insensitive). Unset → derived from `MCP_ENV`: `production` → `INFO`, anything else → `DEBUG`. Bogus values warn at startup and fall through to the env-derived default. |
| `MCP_LOG_PAYLOADS` | optional | both | When truthy (`1`/`true`/`yes`/`on`), the structured-logging middleware includes tool request/response payloads. Default off — payloads can include chat prompts, document bodies, and base64 uploads. Useful for short debugging windows only. |

## Deployment in `foss-server-bundle-devstack`

The MCP server runs as a sidecar on its own subdomain (`https://<prefix>research-mcp.<platform-domain>`). The devstack:

- Auto-derives `COGNITO_USER_POOL_ID` and `COGNITO_AWS_REGION` from `OIDC_BASE_URI` (see `scripts/derive-oidc-endpoints.sh`).
- Wires `SURFSENSE_BASE_URL=http://surfsense-backend:8000` so the MCP → backend call stays on the docker network.
- Wires `MCP_OAUTH_STORAGE_URL=redis://valkey:6379/11` so OAuth state lives in the shared Valkey on a dedicated DB rather than the container filesystem — refresh tokens survive `docker compose down && up`.
- Removes `mpass-auth@docker` from this service's Traefik router (FastMCP is the sole auth layer); keeps `strip-auth-headers@docker` so external clients can't forge `X-Auth-Request-*`.

The only manual step beyond `make dev.up.surfsense` is the one-time AWS Console task described under [How auth works](#how-auth-works).

### Healthcheck

- `GET /healthz` — returns `{"status":"ok"}` unauthenticated. Docker and Traefik liveness probes use this so they never need to forge bearer headers.
- Upstream dependency health (SurfSense backend reachable, Bearer validation working) is exercised only by real `/mcp` calls — `/healthz` is process-liveness only.

## Development

```bash
cd surfsense-mcp-server

# Install with dev extras
uv pip install -e ".[dev]"

# Lint & format
ruff check surfsense_mcp/ tests/
ruff format surfsense_mcp/ tests/

# Tests (no live backend required — httpx is mocked)
pytest

# Coverage
uv run --with coverage --with pytest-cov pytest \
    --cov=surfsense_mcp --cov-report=term-missing

# Upgrade fastmcp (v3 required, < 4)
uv sync --extra dev --upgrade-package fastmcp
```

Tests use an in-memory `httpx.MockTransport` fixture — no running SurfSense instance required. The HTTP-mode auth path (id_token → `X-Auth-Request-Email` injection / id_token Bearer forwarding) is covered by `tests/test_http_mode_auth.py` and `tests/test_auth_http.py`; the id_token claim capture is covered by `tests/test_auth_cognito.py`. These also stub the OIDC discovery doc so the suite never hits the real Cognito service.

See `CLAUDE.md` for tool conventions, FastMCP version notes, and constraints when adding new tools.
