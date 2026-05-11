"""Main entry point for the SurfSense MCP Server."""

import logging
import os
import sys
from enum import Enum

import uvicorn
from moneta_mcp_auth.env import (
    configure_json_logging,
    configure_uvicorn_json_logging,
    resolve_log_level,
    warn_if_storage_missing_in_production,
)
from moneta_mcp_auth.http_app import healthz, resolve_cors_origins, resolve_http_port
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route

from surfsense_mcp.server import get_header_mcp, get_stdio_mcp

REQUIRED_HTTP_ENV_VARS = (
    "MCP_BASE_URL",
    "COGNITO_USER_POOL_ID",
    "COGNITO_AWS_REGION",
    "OIDC_CLIENT_ID",
)

DEFAULT_HTTP_PORT = 8211

__all__ = [
    "DEFAULT_HTTP_PORT",
    "REQUIRED_HTTP_ENV_VARS",
    "ServerMode",
    "healthz",
    "main",
    "resolve_cors_origins",
    "resolve_http_port",
    "resolve_log_level",
    "warn_if_storage_missing_in_production",
]


logger = logging.getLogger("fastmcp.surfsense_mcp")


class ServerMode(Enum):
    STDIO = "stdio"
    HTTP = "http"


def main() -> None:
    """Run the MCP server."""
    configure_json_logging()

    server_mode = ServerMode.STDIO
    if len(sys.argv) > 1:
        server_mode = ServerMode(sys.argv[1])

    if not os.getenv("SURFSENSE_BASE_URL"):
        raise ValueError("SURFSENSE_BASE_URL is not set")

    if server_mode == ServerMode.STDIO:
        has_jwt = bool(os.getenv("SURFSENSE_JWT"))
        has_password_creds = bool(os.getenv("SURFSENSE_EMAIL")) and bool(os.getenv("SURFSENSE_PASSWORD"))
        if not has_jwt and not has_password_creds:
            raise ValueError(
                "stdio mode requires SURFSENSE_JWT, or both SURFSENSE_EMAIL "
                "and SURFSENSE_PASSWORD for the password-login fallback."
            )
        get_stdio_mcp().run()
        return

    if server_mode == ServerMode.HTTP:
        missing = [name for name in REQUIRED_HTTP_ENV_VARS if not os.getenv(name)]
        if missing:
            raise ValueError("http mode is missing required env vars: " + ", ".join(missing))
        # AWSCognitoProvider needs entropy for FastMCP-issued JWTs. Confidential
        # Cognito clients supply OIDC_CLIENT_SECRET (FastMCP derives the signing
        # key from it). Public/PKCE clients have no secret and must supply
        # MCP_JWT_SIGNING_KEY explicitly. One of the two must be set.
        if not os.getenv("OIDC_CLIENT_SECRET") and not os.getenv("MCP_JWT_SIGNING_KEY"):
            raise ValueError(
                "http mode requires OIDC_CLIENT_SECRET (confidential client) "
                "or MCP_JWT_SIGNING_KEY (public/PKCE client)."
            )
        warn_if_storage_missing_in_production(logger=logger)
        header_mcp = get_header_mcp()
        cors = [
            Middleware(
                CORSMiddleware,
                allow_origins=resolve_cors_origins(log=logger),
                allow_credentials=False,
                allow_methods=["*"],
                allow_headers=[
                    "mcp-protocol-version",
                    "mcp-session-id",
                    "Authorization",
                    "Content-Type",
                ],
                expose_headers=["mcp-session-id"],
            )
        ]
        header_app = header_mcp.http_app(middleware=cors, stateless_http=True)

        # AWSCognitoProvider publishes /.well-known/oauth-protected-resource and
        # /.well-known/oauth-authorization-server natively on the mounted app,
        # so we only layer a /healthz probe in front of it.
        app = Starlette(
            routes=[
                Route("/healthz", healthz, methods=["GET"]),
                Mount("/", app=header_app),
            ],
            lifespan=header_app.lifespan,
        )

        level = resolve_log_level()
        configure_uvicorn_json_logging(level)

        port = resolve_http_port(default=DEFAULT_HTTP_PORT, log=logger)
        logger.info("Starting HTTP server on :%d", port)
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level=logging.getLevelName(level).lower(),
            access_log=False,
        )
        return


if __name__ == "__main__":
    main()
