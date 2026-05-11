"""OAuth state storage — re-exports :mod:`moneta_mcp_auth.storage`.

This module is kept as a thin compatibility shim so existing imports
(``from surfsense_mcp.auth.storage import build_oauth_storage``) continue
to work after the refactor. The implementation lives in
:mod:`moneta_mcp_auth.storage`; see that module's docstring for the
Valkey/Fernet design.
"""

from __future__ import annotations

from moneta_mcp_auth.storage import (
    ValkeyConfig,
    build_oauth_storage,
    parse_storage_url,
)

__all__ = ["ValkeyConfig", "build_oauth_storage", "parse_storage_url"]
