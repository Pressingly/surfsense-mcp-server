"""Tool registration for the SurfSense MCP Server.

Implements a discovery pattern: only a small default set of tools is registered
on startup.  Two meta tools — ``list_available_tools`` and ``enable_tools`` —
let the LLM discover the full catalog and dynamically enable additional tools
at runtime.

The ``SURFSENSE_MCP_ENABLED_TOOLS`` environment variable overrides the default
set when provided (comma-separated tool names).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP

from surfsense_mcp.tools.documents import register_document_tools
from surfsense_mcp.tools.logs import register_log_tools
from surfsense_mcp.tools.notes import register_note_tools
from surfsense_mcp.tools.reports import register_report_tools
from surfsense_mcp.tools.search_spaces import register_search_space_tools
from surfsense_mcp.tools.threads import register_thread_tools

logger = logging.getLogger(__name__)

# Tools registered on startup when SURFSENSE_MCP_ENABLED_TOOLS is unset.
DEFAULT_TOOLS: set[str] = {
    "list_search_spaces",
    "search_documents",
    "query_surfsense",
    "list_research_threads",
    "get_document",
}

# Meta tool names — always registered, never in the catalog.
META_TOOLS: set[str] = {"list_available_tools", "enable_tools"}

# Ordered (category_label, register_fn) — drives catalog grouping.
_CATEGORY_REGISTRATIONS: list[tuple[str, Any]] = [
    ("Search Spaces", register_search_space_tools),
    ("Documents", register_document_tools),
    ("Research Threads", register_thread_tools),
    ("Reports", register_report_tools),
    ("Notes", register_note_tools),
    ("Logs", register_log_tools),
]


@dataclass
class _ToolEntry:
    """Catalog entry for a single tool."""

    name: str
    description: str
    category: str
    component: Any  # FunctionTool — kept for re-registration
    enabled: bool = False


@dataclass
class _ToolCatalog:
    """Holds the full tool catalog and the MCP instance for runtime changes."""

    mcp: FastMCP
    entries: dict[str, _ToolEntry] = field(default_factory=dict)


# Module-level singleton populated by ``register_tools``.
_catalog: _ToolCatalog | None = None


def register_tools(mcp: FastMCP) -> None:
    """Register tools with the MCP server using the discovery pattern.

    1. Register ALL tools from every category module.
    2. Build a catalog with names, descriptions, and categories.
    3. Remove all tools except the initial set + meta tools.
    4. Register the two meta tools (``list_available_tools``, ``enable_tools``).
    """
    global _catalog  # noqa: PLW0603
    _catalog = _ToolCatalog(mcp=mcp)

    # --- Phase 1: register everything, capturing per-category membership ---
    for category_label, register_fn in _CATEGORY_REGISTRATIONS:
        before = set(mcp._local_provider._components.keys())
        register_fn(mcp)
        after = set(mcp._local_provider._components.keys())

        for key in sorted(after - before):
            name = key.split(":", 1)[1].rsplit("@", 1)[0]
            comp = mcp._local_provider._components[key]
            _catalog.entries[name] = _ToolEntry(
                name=name,
                description=comp.description or "",
                category=category_label,
                component=comp,
            )

    # --- Phase 2: determine which tools to keep ---
    env_value = os.environ.get("SURFSENSE_MCP_ENABLED_TOOLS", "").strip()
    if env_value:
        enabled_set = {n.strip() for n in env_value.split(",") if n.strip()}
        unknown = enabled_set - set(_catalog.entries)
        if unknown:
            logger.warning(
                "SURFSENSE_MCP_ENABLED_TOOLS contains unknown tool names: %s",
                ", ".join(sorted(unknown)),
            )
    else:
        enabled_set = set(DEFAULT_TOOLS)

    # --- Phase 3: remove tools not in the enabled set ---
    for name, entry in _catalog.entries.items():
        if name in enabled_set:
            entry.enabled = True
        else:
            mcp._local_provider.remove_tool(name)

    kept = sorted(n for n, e in _catalog.entries.items() if e.enabled)
    logger.info(
        "Tool discovery: %d/%d tools enabled on startup: %s",
        len(kept),
        len(_catalog.entries),
        ", ".join(kept),
    )

    # --- Phase 4: register meta tools ---
    _register_meta_tools(mcp)


def _register_meta_tools(mcp: FastMCP) -> None:
    """Register the two meta tools for tool discovery."""

    @mcp.tool()
    async def list_available_tools() -> dict[str, Any]:
        """List ALL available SurfSense MCP tools grouped by category.

        Returns the full catalog of tools with their names, descriptions, and
        whether they are currently enabled.  Use ``enable_tools`` to activate
        additional tools at runtime.
        """
        assert _catalog is not None
        categories: dict[str, list[dict[str, Any]]] = {}
        for entry in _catalog.entries.values():
            categories.setdefault(entry.category, []).append(
                {
                    "name": entry.name,
                    "description": entry.description,
                    "enabled": entry.enabled,
                }
            )
        return {
            "total_tools": len(_catalog.entries),
            "enabled_count": sum(1 for e in _catalog.entries.values() if e.enabled),
            "categories": categories,
        }

    @mcp.tool()
    async def enable_tools(tool_names: list[str]) -> dict[str, Any]:
        """Dynamically enable additional SurfSense MCP tools by name.

        Previously disabled tools are registered so they become callable.
        Already-enabled tools are reported but not re-registered.

        Args:
            tool_names: List of tool names to enable (from ``list_available_tools``).
        """
        assert _catalog is not None
        newly_enabled: list[str] = []
        already_enabled: list[str] = []
        unknown: list[str] = []

        for name in tool_names:
            entry = _catalog.entries.get(name)
            if entry is None:
                unknown.append(name)
                continue
            if entry.enabled:
                already_enabled.append(name)
                continue
            _catalog.mcp._local_provider.add_tool(entry.component)
            entry.enabled = True
            newly_enabled.append(name)

        if newly_enabled:
            logger.info("Dynamically enabled tools: %s", ", ".join(sorted(newly_enabled)))

        return {
            "newly_enabled": sorted(newly_enabled),
            "already_enabled": sorted(already_enabled),
            "unknown": sorted(unknown),
            "total_enabled": sum(1 for e in _catalog.entries.values() if e.enabled),
        }
