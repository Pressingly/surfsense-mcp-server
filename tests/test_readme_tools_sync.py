"""Lock the README's tool list against the actually-registered tools.

The Tools section of the README is the canonical user-facing surface for
discovering what this server exposes. When a tool is renamed, added, or
removed without a README edit, MCP-client setup instructions silently rot
and support triage gets harder. A test is the cheapest place to catch that.

The check is bidirectional: a tool missing from the README is a docs gap,
and a tool documented but never registered is a phantom that sends users
looking for something that does not exist.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastmcp import Client

from surfsense_mcp.server import get_stdio_mcp
from tests.conftest import DELETE_TOOL_NAMES

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"


def _readme_tools_section() -> str:
    """The body between ``## Tools`` and the next top-level header."""
    text = README.read_text(encoding="utf-8")
    start = text.index("## Tools")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else None]


def _documented_tool_names(section: str) -> set[str]:
    """Backticked identifiers from the section's markdown *tables* only.

    Restricted to table rows on purpose. The surrounding prose legitimately
    mentions backticked things that are not tools — the removed meta tools,
    env vars, ``tools/list`` — and a reverse check that swept those up would
    fail on documentation that is in fact correct.
    """
    rows = [line for line in section.splitlines() if line.lstrip().startswith("|")]
    return set(re.findall(r"`([a-z_]+)`", "\n".join(rows)))


async def test_readme_matches_registered_tools() -> None:
    # The autouse _env fixture sets SURFSENSE_ENABLE_DELETE, so this is the
    # full 28-tool surface — which is what the README tables document.
    async with Client(get_stdio_mcp()) as client:
        registered = {tool.name for tool in await client.list_tools()}

    documented = _documented_tool_names(_readme_tools_section())

    missing = registered - documented
    assert not missing, (
        f"README ## Tools section is missing {sorted(missing)} — "
        "rename/add a tool? Update README.md to keep client setup docs in sync."
    )

    phantom = documented - registered
    assert not phantom, (
        f"README ## Tools section documents {sorted(phantom)}, which the server "
        "does not register — remove them, or the docs point at tools that don't exist."
    )


async def test_readme_tool_counts_match_reality() -> None:
    """The hardcoded totals in the Tool listing prose must not drift."""
    async with Client(get_stdio_mcp()) as client:
        registered = {tool.name for tool in await client.list_tools()}

    section = _readme_tools_section()

    total = re.search(r"(\d+) tools across (\d+) categories", section)
    assert total, "README no longer states the total tool/category count"
    assert int(total.group(1)) == len(registered)

    default = re.search(r"the server registers (\d+) tools", section)
    assert default, "README no longer states the default (deletes-off) tool count"
    assert int(default.group(1)) == len(registered - DELETE_TOOL_NAMES)

    # Category count, so adding a module without a README edit is caught too.
    from surfsense_mcp.tools import _CATEGORY_REGISTRATIONS

    assert int(total.group(2)) == len(_CATEGORY_REGISTRATIONS), (
        f"README says {total.group(2)} categories but the server registers "
        f"{len(_CATEGORY_REGISTRATIONS)} — update README.md."
    )
