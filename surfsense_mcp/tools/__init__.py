"""Tool registration for the SurfSense MCP Server.

Every tool the server exposes is registered on startup and returned by
``tools/list``: an LLM sees the real tool list on connect and calls tools
directly, with no runtime discovery or enablement step in between.

The one exception is the destructive ``delete_*`` family, which is opt-in
behind ``SURFSENSE_ENABLE_DELETE`` (see :mod:`surfsense_mcp.config`) and is
not registered at all unless that flag is set.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastmcp import FastMCP
from fastmcp.tools import Tool

from surfsense_mcp.config import delete_tools_enabled
from surfsense_mcp.tools.documents import register_document_tools
from surfsense_mcp.tools.logs import register_log_tools
from surfsense_mcp.tools.notes import register_note_tools
from surfsense_mcp.tools.reports import register_report_tools
from surfsense_mcp.tools.search_spaces import register_search_space_tools
from surfsense_mcp.tools.threads import register_thread_tools

logger = logging.getLogger(__name__)

# Ordered per-category registration functions — one entry per tool module,
# in the order their tools are registered.
_CATEGORY_REGISTRATIONS: list[Callable[[FastMCP], None]] = [
    register_search_space_tools,
    register_document_tools,
    register_thread_tools,
    register_report_tools,
    register_note_tools,
    register_log_tools,
]


def _registered_tool_count(mcp: FastMCP) -> int | None:
    """Number of tools registered on ``mcp``, or None when it can't be read.

    ``list_tools()`` is async while this runs from a synchronous server
    constructor (itself sometimes called from inside a running event loop),
    so the count is read off the local provider's component map instead.
    ``local_provider`` is public API but ``_components`` is a FastMCP
    internal: a future 3.x release may restructure it, and losing a number
    from one startup log line is a far better failure mode than a server
    that cannot boot.
    """
    try:
        components = mcp.local_provider._components
        return sum(1 for c in components.values() if isinstance(c, Tool))
    except (AttributeError, TypeError):  # pragma: no cover - defensive
        return None


def register_tools(mcp: FastMCP) -> None:
    """Register every SurfSense tool with the MCP server."""
    for register_fn in _CATEGORY_REGISTRATIONS:
        register_fn(mcp)

    count = _registered_tool_count(mcp)
    logger.info(
        "Registered %s tool(s) from %d category module(s)",
        count if count is not None else "an unknown number of",
        len(_CATEGORY_REGISTRATIONS),
    )
    # Deletes are off by default, so an upgraded instance loses them silently.
    # Say so at startup rather than leaving a vanished tool as the only signal.
    if not delete_tools_enabled():
        logger.info("Destructive delete tools are DISABLED — set SURFSENSE_ENABLE_DELETE=1 to register them")
