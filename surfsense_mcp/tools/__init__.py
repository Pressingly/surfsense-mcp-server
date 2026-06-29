"""Tool registration for the SurfSense MCP Server."""

import logging
import os

from fastmcp import FastMCP

from surfsense_mcp.tools.documents import register_document_tools
from surfsense_mcp.tools.logs import register_log_tools
from surfsense_mcp.tools.notes import register_note_tools
from surfsense_mcp.tools.reports import register_report_tools
from surfsense_mcp.tools.search_spaces import register_search_space_tools
from surfsense_mcp.tools.threads import register_thread_tools

logger = logging.getLogger(__name__)


def register_tools(mcp: FastMCP) -> None:
    """Register all tools with the MCP server.

    If the ``SURFSENSE_MCP_ENABLED_TOOLS`` environment variable is set to a
    comma-separated list of tool names, only those tools are kept; all others
    are removed after registration.  When the variable is unset or empty every
    tool is registered (backward compatible).
    """
    register_search_space_tools(mcp)
    register_document_tools(mcp)
    register_thread_tools(mcp)
    register_report_tools(mcp)
    register_note_tools(mcp)
    register_log_tools(mcp)

    _filter_tools(mcp)


def _filter_tools(mcp: FastMCP) -> None:
    """Remove tools not listed in SURFSENSE_MCP_ENABLED_TOOLS (if set)."""
    env_value = os.environ.get("SURFSENSE_MCP_ENABLED_TOOLS", "").strip()

    # Read registered tool names directly from the internal components dict.
    # Keys are formatted as "tool:<name>@<version>" by FastMCP's LocalProvider.
    all_tool_names = sorted(
        key.split(":", 1)[1].rsplit("@", 1)[0]
        for key in mcp._local_provider._components
        if key.startswith("tool:")
    )

    if not env_value:
        logger.info(
            "SURFSENSE_MCP_ENABLED_TOOLS not set — all %d tools registered: %s",
            len(all_tool_names),
            ", ".join(all_tool_names),
        )
        return

    enabled = {name.strip() for name in env_value.split(",") if name.strip()}

    # Warn about names that don't match any registered tool.
    unknown = enabled - set(all_tool_names)
    if unknown:
        logger.warning(
            "SURFSENSE_MCP_ENABLED_TOOLS contains unknown tool names: %s",
            ", ".join(sorted(unknown)),
        )

    to_remove = [name for name in all_tool_names if name not in enabled]
    for name in to_remove:
        mcp._local_provider.remove_tool(name)

    kept = sorted(enabled & set(all_tool_names))
    logger.info(
        "SURFSENSE_MCP_ENABLED_TOOLS filtered to %d tool(s): %s",
        len(kept),
        ", ".join(kept),
    )
