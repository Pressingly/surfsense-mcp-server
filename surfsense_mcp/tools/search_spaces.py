"""Search space tools for the SurfSense MCP Server."""

from typing import Any

from fastmcp import FastMCP

from surfsense_mcp.client import authed_request
from surfsense_mcp.config import delete_tools_enabled


def register_search_space_tools(mcp: FastMCP) -> None:
    """Register search-space tools."""

    @mcp.tool()
    async def list_search_spaces(
        owned_only: bool = False,
        skip: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """
        List search spaces the authenticated user can access.

        A search space is a workspace that groups documents, chats, and research
        threads. Most other tools require a `search_space_id`, so this is
        typically the first call.

        Args:
            owned_only: If true, return only spaces the user owns (excludes
                shared spaces they are a member of). Defaults to false.
            skip: Number of items to skip for pagination. Defaults to 0.
            limit: Maximum number of items to return (backend default 200).

        Returns:
            List of search spaces. Each entry contains at least: id, name,
            description, created_at, user_id, citations_enabled,
            qna_custom_instructions, member_count, is_owner.
        """
        response = await authed_request(
            "GET",
            "/api/v1/searchspaces",
            params={
                "owned_only": str(owned_only).lower(),
                "skip": skip,
                "limit": limit,
            },
        )
        return response.json()

    @mcp.tool()
    async def get_search_space(search_space_id: int) -> dict[str, Any]:
        """
        Retrieve a single search space by ID.

        Args:
            search_space_id: The integer search space ID.

        Returns:
            Search space object: id, name, description, created_at, user_id,
            citations_enabled, qna_custom_instructions, shared_memory_md,
            ai_file_sort_enabled.
        """
        response = await authed_request("GET", f"/api/v1/searchspaces/{search_space_id}")
        return response.json()

    @mcp.tool()
    async def create_search_space(
        name: str,
        description: str | None = None,
        citations_enabled: bool = True,
        qna_custom_instructions: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a new search space (knowledge base).

        Requires SEARCH_SPACES_CREATE permission. The calling user becomes the
        owner of the new space.

        Args:
            name: Human-readable name (required).
            description: Optional free-text description.
            citations_enabled: Whether chat responses should include citations.
                Defaults to true (the SurfSense default).
            qna_custom_instructions: Optional custom system-prompt addendum
                used by the chat agent when answering in this space.

        Returns:
            The newly created SearchSpaceRead object.
        """
        body: dict[str, Any] = {"name": name, "citations_enabled": citations_enabled}
        if description is not None:
            body["description"] = description
        if qna_custom_instructions is not None:
            body["qna_custom_instructions"] = qna_custom_instructions
        response = await authed_request("POST", "/api/v1/searchspaces", json=body)
        return response.json()

    @mcp.tool()
    async def update_search_space(
        search_space_id: int,
        name: str | None = None,
        description: str | None = None,
        citations_enabled: bool | None = None,
        qna_custom_instructions: str | None = None,
        shared_memory_md: str | None = None,
        ai_file_sort_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """
        Update a search space. Only owners (or members with update rights) can
        call this.

        All args are optional — only fields you pass are sent.

        Returns:
            The updated SearchSpaceRead object.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if citations_enabled is not None:
            body["citations_enabled"] = citations_enabled
        if qna_custom_instructions is not None:
            body["qna_custom_instructions"] = qna_custom_instructions
        if shared_memory_md is not None:
            body["shared_memory_md"] = shared_memory_md
        if ai_file_sort_enabled is not None:
            body["ai_file_sort_enabled"] = ai_file_sort_enabled
        if not body:
            raise ValueError("update_search_space requires at least one field to update")
        response = await authed_request("PUT", f"/api/v1/searchspaces/{search_space_id}", json=body)
        return response.json()

    if delete_tools_enabled():

        @mcp.tool()
        async def delete_search_space(search_space_id: int) -> dict[str, Any]:
            """
            Delete a search space and ALL its documents, threads, and reports.

            Irreversible. Requires owner-level access.

            Returns:
                `{"message": "..."}` confirmation from the backend.
            """
            response = await authed_request("DELETE", f"/api/v1/searchspaces/{search_space_id}")
            return response.json()
