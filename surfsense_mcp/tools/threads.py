"""Research-thread (chat) tools for the SurfSense MCP Server."""

from __future__ import annotations

import json as _json
from typing import Any

from fastmcp import FastMCP
from fastmcp.utilities.logging import get_logger

from surfsense_mcp.client import authed_request, stream_authed_post
from surfsense_mcp.config import delete_tools_enabled

logger = get_logger(__name__)

_TEXT_DELTA_EVENTS = {"text-delta"}
_ALT_TEXT_DELTA_EVENTS = {"data-text-delta"}
_KNOWN_CONTROL_EVENTS = {
    "start",
    "start-step",
    "finish",
    "finish-step",
    "text-start",
    "text-end",
    "data-thinking-step",
    "data-thread-title-update",
}


def register_thread_tools(mcp: FastMCP) -> None:
    """Register research-thread tools."""

    @mcp.tool()
    async def list_research_threads(
        search_space_id: int,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """
        List chat/research threads in a search space (active + archived).

        SurfSense's "research" surface is its chat agent; every chat thread is
        effectively a research session, so this tool covers the
        research_history use case.

        Args:
            search_space_id: The search space to list threads for (required).
            limit: Optional cap on active threads returned. Archived threads
                are always returned in full.

        Returns:
            Object with keys: threads (active, list of thread summaries) and
            archived_threads (list of archived thread summaries). Each thread
            summary contains: id, title, archived, visibility, created_by_id,
            is_own_thread, created_at, updated_at.
        """
        params: dict[str, Any] = {"search_space_id": search_space_id}
        if limit is not None:
            params["limit"] = limit
        response = await authed_request("GET", "/api/v1/threads", params=params)
        return response.json()

    @mcp.tool()
    async def get_research_thread(thread_id: int) -> dict[str, Any]:
        """
        Retrieve a single research thread including its full message history.

        Args:
            thread_id: The integer thread ID (as returned by
                list_research_threads).

        Returns:
            Thread object with messages list. Each message has: id, role
            (user|assistant|system), content (rich JSONB), thread_id,
            author_id, created_at.
        """
        response = await authed_request("GET", f"/api/v1/threads/{thread_id}")
        return response.json()

    if delete_tools_enabled():

        @mcp.tool()
        async def delete_research_thread(thread_id: int) -> dict[str, Any]:
            """
            Permanently delete a research thread and its messages. Only the
            thread creator (or a user with thread-delete permission in the
            space) can call this.

            Returns:
                Backend confirmation: `{"message": "..."}`.
            """
            response = await authed_request("DELETE", f"/api/v1/threads/{thread_id}")
            return response.json()

    @mcp.tool()
    async def get_thread_messages(thread_id: int) -> list[dict[str, Any]]:
        """
        Get the raw message list for a thread (an alternate view of
        get_research_thread that returns messages directly).

        Returns:
            List of NewChatMessageRead objects: id, role, content, thread_id,
            author_id, created_at.
        """
        response = await authed_request("GET", f"/api/v1/threads/{thread_id}/messages")
        return response.json()

    @mcp.tool()
    async def query_surfsense(
        user_query: str,
        search_space_id: int,
        thread_id: int | None = None,
        mentioned_document_ids: list[int] | None = None,
        disabled_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Ask SurfSense's research agent a question and stream the full
        response to completion.

        If `thread_id` is omitted, a new thread is created in the given
        search space first (titled with the first 80 chars of the query).

        The underlying `POST /api/v1/new_chat` endpoint streams Server-Sent
        Events in the Vercel AI SDK format. This tool consumes the full
        stream, concatenates every `text-delta` event's `delta` into a single
        string, and returns the final answer. Control events (`start`,
        `finish`, `text-start`, etc.) are skipped silently. Unknown events
        are logged at DEBUG but do not fail the call.

        Args:
            user_query: Natural-language question to ask.
            search_space_id: Search space whose documents the agent should
                draw on.
            thread_id: Optional existing thread ID. Omit to start a new
                conversation.
            mentioned_document_ids: Optional document IDs to explicitly
                reference (equivalent of @-mentioning docs in the UI).
            disabled_tools: Optional list of agent-tool names the user wants
                the agent to not use.

        Returns:
            `{"thread_id": int, "response": str, "raw_event_count": int}`.
        """
        resolved_thread_id = thread_id
        if resolved_thread_id is None:
            title = (user_query[:80] or "New Chat").strip() or "New Chat"
            create_resp = await authed_request(
                "POST",
                "/api/v1/threads",
                json={"search_space_id": search_space_id, "title": title},
            )
            resolved_thread_id = int(create_resp.json()["id"])

        payload: dict[str, Any] = {
            "chat_id": resolved_thread_id,
            "user_query": user_query,
            "search_space_id": search_space_id,
        }
        if mentioned_document_ids:
            payload["mentioned_document_ids"] = mentioned_document_ids
        if disabled_tools:
            payload["disabled_tools"] = disabled_tools

        full_response: list[str] = []
        event_count = 0
        async with stream_authed_post("/api/v1/new_chat", json=payload) as response:
            async for line in response.aiter_lines():
                event_count += 1
                delta = _extract_text_delta(line)
                if delta:
                    full_response.append(delta)

        return {
            "thread_id": resolved_thread_id,
            "response": "".join(full_response),
            "raw_event_count": event_count,
        }


def _extract_text_delta(line: str) -> str | None:
    """Parse a single SSE line into a text delta (or None if not content).

    SurfSense/Vercel AI SDK emits events in the shape:
        data: {"type":"text-delta","id":"...","delta":"some text"}
    Control lines (event: ..., empty keepalives) are skipped. Unknown JSON
    shapes are ignored and logged at DEBUG.
    """
    if not line:
        return None
    # SSE framing: only ``data:`` carries payload. ``event:``/``id:``/``retry:``
    # are metadata; a line starting with ``:`` is a comment (commonly used for
    # keepalive pings). Skipping these prevents raw framing like ``id: 3``
    # from falling through JSON parsing and getting appended as response text.
    if line.startswith((":", "event:", "id:", "retry:")):
        return None

    if line.startswith("data:"):
        chunk = line[5:].strip()
    else:
        chunk = line.strip()

    if not chunk or chunk == "[DONE]":
        return None

    try:
        event = _json.loads(chunk)
    except _json.JSONDecodeError:
        # Raw text fallback — some endpoints emit plain text chunks.
        return chunk

    if not isinstance(event, dict):
        return str(event)

    etype = event.get("type", "")

    if etype in _TEXT_DELTA_EVENTS:
        delta = event.get("delta") or event.get("textDelta")
        if isinstance(delta, str):
            return delta

    if etype in _ALT_TEXT_DELTA_EVENTS:
        data = event.get("data") or {}
        if isinstance(data, dict):
            return data.get("textDelta") or data.get("text") or None

    if "content" in event and etype not in _KNOWN_CONTROL_EVENTS:
        value = event["content"]
        if isinstance(value, str):
            return value

    if "text" in event and etype not in _KNOWN_CONTROL_EVENTS and etype not in _TEXT_DELTA_EVENTS:
        value = event["text"]
        if isinstance(value, str):
            return value

    if etype in _KNOWN_CONTROL_EVENTS:
        return None

    logger.debug("Unhandled SSE event shape: keys=%s", list(event.keys()))
    return None
