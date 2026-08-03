"""Note tools for the SurfSense MCP Server."""

from typing import Any

from fastmcp import FastMCP

from surfsense_mcp.client import authed_request
from surfsense_mcp.config import delete_tools_enabled


def register_note_tools(mcp: FastMCP) -> None:
    """Register note tools."""

    @mcp.tool()
    async def create_note(
        search_space_id: int,
        title: str,
        source_markdown: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a new note document in a search space.

        Notes are stored as Documents with `document_type=NOTE`. The `content`
        field is populated the first time the note is saved/reindexed in the
        UI; on creation the backend only requires the title (and optionally
        some markdown to seed the source).

        Args:
            search_space_id: Space that will own the note.
            title: Human-readable title (required, non-empty).
            source_markdown: Optional initial markdown body. May be left
                empty and filled in later.

        Returns:
            The created DocumentRead object: id, title, document_type,
            content, search_space_id, created_at, updated_at, ...
        """
        if not title or not title.strip():
            raise ValueError("title is required")
        body: dict[str, Any] = {"title": title}
        if source_markdown is not None:
            body["source_markdown"] = source_markdown
        response = await authed_request(
            "POST",
            f"/api/v1/search-spaces/{search_space_id}/notes",
            json=body,
        )
        return response.json()

    if delete_tools_enabled():

        @mcp.tool()
        async def delete_note(search_space_id: int, note_id: int) -> dict[str, Any]:
            """
            Permanently delete a note document.

            Returns:
                Backend confirmation.
            """
            response = await authed_request("DELETE", f"/api/v1/search-spaces/{search_space_id}/notes/{note_id}")
            return response.json()
