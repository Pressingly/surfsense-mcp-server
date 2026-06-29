"""Smoke tests for every MCP tool — verify URL, method, body, and auth header."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

from surfsense_mcp import auth as auth_module
from surfsense_mcp.server import get_stdio_mcp
from tests.conftest import FAKE_JWT, json_response


def _assert_bearer(request: httpx.Request, token: str = FAKE_JWT) -> None:
    assert request.headers.get("Authorization") == f"Bearer {token}"


async def _call_tool(tool_name: str, arguments: dict) -> dict | list:
    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        result = await client.call_tool(tool_name, arguments)
    assert result.content, "tool returned no content"
    text = result.content[0].text  # type: ignore[attr-defined]
    return json.loads(text)


async def _call_tool_raw(tool_name: str, arguments: dict) -> str:
    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        result = await client.call_tool(tool_name, arguments)
    assert result.content
    return result.content[0].text  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Search spaces
# ---------------------------------------------------------------------------


async def test_list_search_spaces(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response([{"id": 1, "name": "default", "is_owner": True}]))
    data = await _call_tool("list_search_spaces", {"owned_only": True, "limit": 50})
    assert data == [{"id": 1, "name": "default", "is_owner": True}]
    req = recorded[0]
    assert req.url.path == "/api/v1/searchspaces"
    assert req.url.params["owned_only"] == "true"
    assert req.url.params["limit"] == "50"
    assert req.url.params["skip"] == "0"
    _assert_bearer(req)


async def test_get_search_space(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"id": 7, "name": "research"}))
    data = await _call_tool("get_search_space", {"search_space_id": 7})
    assert data["id"] == 7
    req = recorded[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/searchspaces/7"
    _assert_bearer(req)


async def test_create_search_space(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"id": 42, "name": "new"}, status_code=200))
    data = await _call_tool(
        "create_search_space",
        {"name": "new", "description": "hello", "citations_enabled": False},
    )
    assert data["id"] == 42
    req = recorded[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/searchspaces"
    body = json.loads(req.content.decode())
    assert body == {
        "name": "new",
        "citations_enabled": False,
        "description": "hello",
    }
    _assert_bearer(req)


async def test_update_search_space(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"id": 7, "name": "renamed"}))
    await _call_tool(
        "update_search_space",
        {"search_space_id": 7, "name": "renamed", "shared_memory_md": "# notes"},
    )
    req = recorded[0]
    assert req.method == "PUT"
    assert req.url.path == "/api/v1/searchspaces/7"
    body = json.loads(req.content.decode())
    assert body == {"name": "renamed", "shared_memory_md": "# notes"}


async def test_update_search_space_requires_at_least_one_field(mock_transport) -> None:
    mock_transport(lambda req: json_response({}))
    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc:
            await client.call_tool("update_search_space", {"search_space_id": 7})
    assert "at least one field" in str(exc.value)


async def test_delete_search_space(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"message": "deleted"}))
    data = await _call_tool("delete_search_space", {"search_space_id": 7})
    assert data == {"message": "deleted"}
    req = recorded[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/searchspaces/7"


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


async def test_list_documents(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"items": [], "total": 0, "page": 1, "page_size": 50}))
    await _call_tool("list_documents", {"search_space_id": 3, "page_size": 10})
    req = recorded[0]
    assert req.url.path == "/api/v1/documents"
    assert req.url.params["search_space_id"] == "3"
    assert req.url.params["sort_by"] == "created_at"
    assert req.url.params["sort_order"] == "desc"
    assert req.url.params["page_size"] == "10"


async def test_search_documents(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"items": [{"id": 42, "title": "Q3 roadmap"}], "total": 1}))
    data = await _call_tool(
        "search_documents",
        {"title": "roadmap", "search_space_id": 7, "page_size": 5},
    )
    assert data["items"][0]["id"] == 42
    req = recorded[0]
    assert req.url.path == "/api/v1/documents/search"
    assert req.url.params["title"] == "roadmap"
    assert req.url.params["search_space_id"] == "7"
    assert req.url.params["page_size"] == "5"
    _assert_bearer(req)


async def test_get_document(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"id": 42, "title": "Q3 roadmap", "content": "..."}))
    data = await _call_tool("get_document", {"document_id": 42})
    assert data["id"] == 42
    req = recorded[0]
    assert req.url.path == "/api/v1/documents/42"
    _assert_bearer(req)


async def test_get_recent_documents(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"items": [], "total": 0, "page": 1, "page_size": 5}))
    await _call_tool("get_recent_documents", {"search_space_id": 7, "limit": 5})
    req = recorded[0]
    assert req.url.path == "/api/v1/documents"
    assert req.url.params["search_space_id"] == "7"
    assert req.url.params["sort_by"] == "created_at"
    assert req.url.params["sort_order"] == "desc"
    assert req.url.params["page_size"] == "5"
    _assert_bearer(req)


async def test_upload_document(mock_transport, tmp_path) -> None:
    recorded = mock_transport(
        lambda req: json_response({"document_ids": [99], "message": "queued", "status": "pending"})
    )
    file_path = tmp_path / "notes.md"
    file_path.write_text("# hello\n", encoding="utf-8")

    data = await _call_tool(
        "upload_document",
        {
            "file_path": str(file_path),
            "search_space_id": 7,
            "should_summarize": False,
        },
    )
    assert data["document_ids"] == [99]

    req = recorded[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/documents/fileupload"
    content_type = req.headers.get("content-type", "")
    assert content_type.startswith("multipart/form-data"), content_type
    raw = req.content.decode("utf-8", errors="replace")
    assert "search_space_id" in raw
    assert "notes.md" in raw
    assert "# hello" in raw
    _assert_bearer(req)


async def test_upload_document_content(mock_transport) -> None:
    import base64

    recorded = mock_transport(
        lambda req: json_response({"document_ids": [101], "message": "queued", "status": "pending"})
    )
    raw = b"%PDF-1.4 fake pdf bytes"
    encoded = base64.b64encode(raw).decode("ascii")

    data = await _call_tool(
        "upload_document_content",
        {
            "filename": "quarterly.pdf",
            "content_base64": encoded,
            "search_space_id": 7,
            "should_summarize": False,
        },
    )
    assert data["document_ids"] == [101]

    req = recorded[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/documents/fileupload"
    content_type = req.headers.get("content-type", "")
    assert content_type.startswith("multipart/form-data"), content_type
    body = req.content
    assert b"quarterly.pdf" in body
    assert raw in body
    assert b"search_space_id" in body
    _assert_bearer(req)


async def test_upload_document_content_strips_data_url_prefix(mock_transport) -> None:
    import base64

    recorded = mock_transport(
        lambda req: json_response({"document_ids": [102], "message": "queued", "status": "pending"})
    )
    raw = b"hello world"
    encoded = "data:text/plain;base64," + base64.b64encode(raw).decode("ascii")

    await _call_tool(
        "upload_document_content",
        {
            "filename": "greet.txt",
            "content_base64": encoded,
            "search_space_id": 3,
        },
    )
    assert raw in recorded[0].content


async def test_upload_document_content_rejects_bad_base64(mock_transport) -> None:
    mock_transport(lambda req: json_response({}))
    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc:
            await client.call_tool(
                "upload_document_content",
                {
                    "filename": "bad.pdf",
                    "content_base64": "!!!not-base64!!!",
                    "search_space_id": 3,
                },
            )
    assert "base64" in str(exc.value).lower()


async def test_upload_document_retries_with_full_body_on_401(
    mock_transport, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streaming uploads (file handle, not bytes) must be rewound between
    401 retry attempts — otherwise the second POST sends an empty body."""
    monkeypatch.delenv("SURFSENSE_JWT", raising=False)
    monkeypatch.setenv("SURFSENSE_EMAIL", "user@example.com")
    monkeypatch.setenv("SURFSENSE_PASSWORD", "hunter2")
    auth_module.invalidate_password_token()

    file_path = tmp_path / "notes.md"
    file_path.write_text("# hello streaming world\n", encoding="utf-8")

    upload_attempts: list[bytes] = []
    api_counter = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/auth/jwt/login":
            n = len(upload_attempts) + 1
            return httpx.Response(200, json={"access_token": f"token-v{n}", "token_type": "bearer"})
        if req.url.path == "/api/v1/documents/fileupload":
            api_counter["n"] += 1
            upload_attempts.append(req.content)
            if api_counter["n"] == 1:
                return httpx.Response(401, json={"detail": "expired"})
            return httpx.Response(200, json={"document_ids": [1], "status": "ok"})
        return httpx.Response(500, json={"detail": "unexpected"})

    mock_transport(handler)

    data = await _call_tool(
        "upload_document",
        {"file_path": str(file_path), "search_space_id": 7, "should_summarize": False},
    )
    assert data["document_ids"] == [1]
    assert len(upload_attempts) == 2
    # Both attempts must carry the actual file payload — the retry would
    # silently send an empty body if the file handle weren't rewound.
    for body in upload_attempts:
        assert b"# hello streaming world" in body


async def test_upload_document_rejects_oversized_file(mock_transport, tmp_path, monkeypatch) -> None:
    from surfsense_mcp.tools import documents as documents_module

    monkeypatch.setattr(documents_module, "MAX_UPLOAD_BYTES", 1024)

    recorded = mock_transport(lambda req: json_response({}))
    big = tmp_path / "big.bin"
    with big.open("wb") as fh:
        fh.seek(2048)
        fh.write(b"\0")

    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc:
            await client.call_tool(
                "upload_document",
                {"file_path": str(big), "search_space_id": 7},
            )
    assert "exceeds" in str(exc.value).lower()
    assert recorded == []


async def test_upload_document_content_rejects_oversized_payload(mock_transport, monkeypatch) -> None:
    import base64

    from surfsense_mcp.tools import documents as documents_module

    monkeypatch.setattr(documents_module, "MAX_UPLOAD_BYTES", 1024)

    recorded = mock_transport(lambda req: json_response({}))
    encoded = base64.b64encode(b"x" * 2048).decode("ascii")

    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc:
            await client.call_tool(
                "upload_document_content",
                {
                    "filename": "big.pdf",
                    "content_base64": encoded,
                    "search_space_id": 7,
                },
            )
    assert "exceeds" in str(exc.value).lower()
    assert recorded == []


async def test_update_document(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"id": 5, "title": "x"}))
    await _call_tool(
        "update_document",
        {
            "document_id": 5,
            "document_type": "NOTE",
            "content": "new body",
            "search_space_id": 3,
        },
    )
    req = recorded[0]
    assert req.method == "PUT"
    assert req.url.path == "/api/v1/documents/5"
    body = json.loads(req.content.decode())
    assert body == {
        "document_type": "NOTE",
        "content": "new body",
        "search_space_id": 3,
    }


async def test_delete_document(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"message": "deleted"}))
    data = await _call_tool("delete_document", {"document_id": 42})
    assert data == {"message": "deleted"}
    req = recorded[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/documents/42"


async def test_get_document_status(mock_transport) -> None:
    recorded = mock_transport(
        lambda req: json_response({"items": [{"id": 1, "title": "a", "status": {"state": "ready"}}]})
    )
    data = await _call_tool(
        "get_document_status",
        {"search_space_id": 3, "document_ids": "1,2,3"},
    )
    assert data["items"][0]["id"] == 1
    req = recorded[0]
    assert req.url.path == "/api/v1/documents/status"
    assert req.url.params["search_space_id"] == "3"
    assert req.url.params["document_ids"] == "1,2,3"


async def test_get_document_type_counts(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"NOTE": 4, "FILE": 10}))
    data = await _call_tool("get_document_type_counts", {"search_space_id": 3})
    assert data == {"NOTE": 4, "FILE": 10}
    req = recorded[0]
    assert req.url.path == "/api/v1/documents/type-counts"
    assert req.url.params["search_space_id"] == "3"


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


async def test_list_research_threads(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"threads": [], "archived_threads": []}))
    await _call_tool("list_research_threads", {"search_space_id": 7, "limit": 25})
    req = recorded[0]
    assert req.url.path == "/api/v1/threads"
    assert req.url.params["search_space_id"] == "7"
    assert req.url.params["limit"] == "25"
    _assert_bearer(req)


async def test_get_research_thread(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"id": 99, "messages": []}))
    data = await _call_tool("get_research_thread", {"thread_id": 99})
    assert data["id"] == 99
    req = recorded[0]
    assert req.url.path == "/api/v1/threads/99"
    _assert_bearer(req)


async def test_delete_research_thread(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"message": "deleted"}))
    await _call_tool("delete_research_thread", {"thread_id": 99})
    req = recorded[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/threads/99"


async def test_get_thread_messages(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response([{"id": 1, "role": "user", "content": "hi"}]))
    data = await _call_tool("get_thread_messages", {"thread_id": 99})
    assert data[0]["role"] == "user"
    req = recorded[0]
    assert req.url.path == "/api/v1/threads/99/messages"


async def test_query_surfsense_streams_and_creates_thread(mock_transport) -> None:
    sse_body = (
        'data: {"type":"start"}\n\n'
        'data: {"type":"text-delta","id":"t1","delta":"Hello "}\n\n'
        'data: {"type":"text-delta","id":"t1","delta":"world"}\n\n'
        'data: {"type":"finish"}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/threads" and req.method == "POST":
            return httpx.Response(200, json={"id": 555, "title": "new", "search_space_id": 3})
        if req.url.path == "/api/v1/new_chat" and req.method == "POST":
            return httpx.Response(
                200,
                content=sse_body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404, json={"detail": f"unexpected {req.method} {req.url.path}"})

    recorded = mock_transport(handler)

    data = await _call_tool(
        "query_surfsense",
        {"user_query": "What's new?", "search_space_id": 3},
    )

    assert data["thread_id"] == 555
    assert data["response"] == "Hello world"
    assert data["raw_event_count"] > 0

    methods_paths = [(r.method, r.url.path) for r in recorded]
    assert ("POST", "/api/v1/threads") in methods_paths
    assert ("POST", "/api/v1/new_chat") in methods_paths

    chat_req = next(r for r in recorded if r.url.path == "/api/v1/new_chat")
    body = json.loads(chat_req.content.decode())
    assert body["chat_id"] == 555
    assert body["user_query"] == "What's new?"
    assert body["search_space_id"] == 3


async def test_query_surfsense_skips_sse_metadata_lines(mock_transport) -> None:
    """SSE streams interleave id:/retry:/: comment lines with data: frames.
    Those are framing, not payload — they must not leak into the response."""
    sse_body = (
        ": keep-alive ping\n\n"
        "retry: 5000\n\n"
        "id: 3\n"
        'data: {"type":"text-delta","id":"t1","delta":"Hello "}\n\n'
        "id: 4\n"
        'data: {"type":"text-delta","id":"t1","delta":"world"}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/new_chat":
            return httpx.Response(
                200,
                content=sse_body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(500, json={"detail": "should not be called"})

    mock_transport(handler)
    data = await _call_tool(
        "query_surfsense",
        {"user_query": "hi", "search_space_id": 3, "thread_id": 42},
    )
    assert data["response"] == "Hello world"


async def test_query_surfsense_uses_existing_thread(mock_transport) -> None:
    sse_body = 'data: {"type":"text-delta","id":"1","delta":"ok"}\n\ndata: [DONE]\n\n'

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/new_chat":
            return httpx.Response(
                200,
                content=sse_body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(500, json={"detail": "should not be called"})

    recorded = mock_transport(handler)
    data = await _call_tool(
        "query_surfsense",
        {"user_query": "again", "search_space_id": 3, "thread_id": 42},
    )
    assert data["thread_id"] == 42
    assert data["response"] == "ok"
    assert all(r.url.path == "/api/v1/new_chat" for r in recorded)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


async def test_list_reports(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response([{"id": 1, "title": "Weekly"}]))
    data = await _call_tool("list_reports", {"search_space_id": 3, "limit": 10})
    assert data[0]["id"] == 1
    req = recorded[0]
    assert req.url.path == "/api/v1/reports"
    assert req.url.params["search_space_id"] == "3"
    assert req.url.params["limit"] == "10"


async def test_get_report(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"id": 5, "title": "t"}))
    await _call_tool("get_report", {"report_id": 5})
    assert recorded[0].url.path == "/api/v1/reports/5"


async def test_get_report_content(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"id": 5, "content": "# hi", "content_type": "markdown"}))
    data = await _call_tool("get_report_content", {"report_id": 5})
    assert data["content"] == "# hi"
    assert recorded[0].url.path == "/api/v1/reports/5/content"


async def test_export_report(mock_transport) -> None:
    recorded = mock_transport(
        lambda req: httpx.Response(
            200,
            content=b"%PDF-1.4 binary bytes here",
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="report-5.pdf"',
            },
        )
    )
    data = await _call_tool("export_report", {"report_id": 5, "format": "pdf"})
    assert data["content_type"] == "application/pdf"
    assert data["filename"] == "report-5.pdf"
    assert data["size_bytes"] == len(b"%PDF-1.4 binary bytes here")
    req = recorded[0]
    assert req.url.path == "/api/v1/reports/5/export"
    assert req.url.params["format"] == "pdf"


async def test_export_report_streams_size_from_chunks_without_content_length(mock_transport) -> None:
    """When the upstream omits Content-Length we must count bytes on the
    wire rather than buffering the full export in memory."""
    payload = b"X" * 4096

    async def astream():
        yield payload[:1024]
        yield payload[1024:3072]
        yield payload[3072:]

    def handler(req: httpx.Request) -> httpx.Response:
        # httpx.Response auto-derives Content-Length from `content=bytes`.
        # An async iterator leaves Content-Length unset, exercising the
        # chunk-counting fallback path.
        return httpx.Response(
            200,
            content=astream(),
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="big.pdf"',
            },
        )

    mock_transport(handler)
    data = await _call_tool("export_report", {"report_id": 9, "format": "pdf"})
    assert data["size_bytes"] == len(payload)
    assert data["filename"] == "big.pdf"
    assert data["content_type"] == "application/pdf"


async def test_export_report_retries_once_on_401_in_password_mode(
    mock_transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale cached password token → /export 401 → invalidate → re-login →
    retry. Without sharing the retry helper, exports are the one tool that
    silently fails in long-running stdio sessions."""
    monkeypatch.delenv("SURFSENSE_JWT", raising=False)
    monkeypatch.setenv("SURFSENSE_EMAIL", "user@example.com")
    monkeypatch.setenv("SURFSENSE_PASSWORD", "hunter2")
    auth_module.invalidate_password_token()

    login_counter = {"n": 0}
    export_counter = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/auth/jwt/login":
            login_counter["n"] += 1
            return httpx.Response(
                200,
                json={"access_token": f"token-v{login_counter['n']}", "token_type": "bearer"},
            )
        if req.url.path == "/api/v1/reports/5/export":
            export_counter["n"] += 1
            if export_counter["n"] == 1:
                assert req.headers["Authorization"] == "Bearer token-v1"
                return httpx.Response(401, json={"detail": "expired"})
            assert req.headers["Authorization"] == "Bearer token-v2"
            return httpx.Response(
                200,
                content=b"%PDF-1.4 retry-payload",
                headers={
                    "content-type": "application/pdf",
                    "content-disposition": 'attachment; filename="r.pdf"',
                },
            )
        return httpx.Response(500, json={"detail": "unexpected"})

    mock_transport(handler)
    data = await _call_tool("export_report", {"report_id": 5, "format": "pdf"})
    assert data["filename"] == "r.pdf"
    assert data["size_bytes"] == len(b"%PDF-1.4 retry-payload")
    assert login_counter["n"] == 2
    assert export_counter["n"] == 2


async def test_export_report_rejects_bad_format(mock_transport) -> None:
    mock_transport(lambda req: json_response({}))
    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc:
            await client.call_tool("export_report", {"report_id": 5, "format": "xlsx"})
    assert "Unsupported export format" in str(exc.value)


async def test_delete_report(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"message": "deleted"}))
    await _call_tool("delete_report", {"report_id": 5})
    req = recorded[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/reports/5"


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


async def test_create_note(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"id": 88, "title": "my note", "document_type": "NOTE"}))
    data = await _call_tool(
        "create_note",
        {"search_space_id": 3, "title": "my note", "source_markdown": "# body"},
    )
    assert data["id"] == 88
    req = recorded[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/search-spaces/3/notes"
    body = json.loads(req.content.decode())
    assert body == {"title": "my note", "source_markdown": "# body"}


async def test_create_note_rejects_empty_title(mock_transport) -> None:
    mock_transport(lambda req: json_response({}))
    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc:
            await client.call_tool("create_note", {"search_space_id": 3, "title": "   "})
    assert "title is required" in str(exc.value)


async def test_delete_note(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response({"message": "deleted"}))
    await _call_tool("delete_note", {"search_space_id": 3, "note_id": 88})
    req = recorded[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/search-spaces/3/notes/88"


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


async def test_get_logs(mock_transport) -> None:
    recorded = mock_transport(lambda req: json_response([{"id": 1, "level": "INFO"}]))
    data = await _call_tool(
        "get_logs",
        {
            "search_space_id": 3,
            "level": "ERROR",
            "source": "etl",
            "limit": 25,
        },
    )
    assert data[0]["id"] == 1
    req = recorded[0]
    assert req.url.path == "/api/v1/logs"
    assert req.url.params["search_space_id"] == "3"
    assert req.url.params["level"] == "ERROR"
    assert req.url.params["source"] == "etl"
    assert req.url.params["limit"] == "25"


# ---------------------------------------------------------------------------
# Error surfaces + auth fallbacks
# ---------------------------------------------------------------------------


async def test_upstream_401_surfaces_as_tool_error(mock_transport) -> None:
    mock_transport(lambda req: httpx.Response(401, json={"detail": "Unauthorized"}))
    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        with pytest.raises(Exception) as exc_info:
            await client.call_tool("get_document", {"document_id": 1})
    assert "401" in str(exc_info.value) or "Unauthorized" in str(exc_info.value)


async def test_password_login_fallback(mock_transport, monkeypatch: pytest.MonkeyPatch) -> None:
    """If SURFSENSE_JWT is unset but email+password are, the client should
    POST /auth/jwt/login first, then use the returned token for the actual
    request."""
    monkeypatch.delenv("SURFSENSE_JWT", raising=False)
    monkeypatch.setenv("SURFSENSE_EMAIL", "user@example.com")
    monkeypatch.setenv("SURFSENSE_PASSWORD", "hunter2")
    auth_module.invalidate_password_token()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/auth/jwt/login":
            # fastapi-users expects form-encoded username/password
            assert req.headers.get("content-type", "").startswith("application/x-www-form-urlencoded")
            body = req.content.decode()
            assert "username=user%40example.com" in body
            assert "password=hunter2" in body
            return httpx.Response(200, json={"access_token": "logged-in-token", "token_type": "bearer"})
        if req.url.path == "/api/v1/searchspaces":
            assert req.headers.get("Authorization") == "Bearer logged-in-token"
            return httpx.Response(200, json=[{"id": 1, "name": "ok"}])
        return httpx.Response(500, json={"detail": f"unexpected {req.url.path}"})

    recorded = mock_transport(handler)
    data = await _call_tool("list_search_spaces", {})
    assert data[0]["name"] == "ok"
    paths = [r.url.path for r in recorded]
    assert paths[0] == "/auth/jwt/login"
    assert "/api/v1/searchspaces" in paths


async def test_password_login_retries_once_on_401(mock_transport, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale cached password token → 401 → invalidate → re-login → retry."""
    monkeypatch.delenv("SURFSENSE_JWT", raising=False)
    monkeypatch.setenv("SURFSENSE_EMAIL", "user@example.com")
    monkeypatch.setenv("SURFSENSE_PASSWORD", "hunter2")
    auth_module.invalidate_password_token()

    login_counter = {"n": 0}
    api_counter = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/auth/jwt/login":
            login_counter["n"] += 1
            token = f"token-v{login_counter['n']}"
            return httpx.Response(200, json={"access_token": token, "token_type": "bearer"})
        if req.url.path == "/api/v1/documents/99":
            api_counter["n"] += 1
            auth = req.headers.get("Authorization")
            # First call: stale cached token v1 → return 401 to trigger retry.
            if api_counter["n"] == 1:
                assert auth == "Bearer token-v1"
                return httpx.Response(401, json={"detail": "expired"})
            # Second call: fresh token v2 → return success.
            assert auth == "Bearer token-v2"
            return httpx.Response(200, json={"id": 99, "title": "recovered"})
        return httpx.Response(500, json={"detail": "unexpected"})

    mock_transport(handler)

    data = await _call_tool("get_document", {"document_id": 99})
    assert data["id"] == 99
    assert login_counter["n"] == 2
    assert api_counter["n"] == 2


# ---------------------------------------------------------------------------
# Tool discovery — list_available_tools / enable_tools
# ---------------------------------------------------------------------------


async def test_list_available_tools_returns_all_categories(mock_transport) -> None:
    """list_available_tools returns the full catalog grouped by category."""
    mock_transport(lambda req: json_response({}))
    data = await _call_tool("list_available_tools", {})
    assert data["total_tools"] == 28
    assert set(data["categories"].keys()) == {
        "Search Spaces",
        "Documents",
        "Research Threads",
        "Reports",
        "Notes",
        "Logs",
    }
    # Every tool should be enabled because _env sets ALL_TOOL_NAMES.
    assert data["enabled_count"] == 28


async def test_default_tools_only_when_env_unset(monkeypatch, mock_transport) -> None:
    """Without SURFSENSE_MCP_ENABLED_TOOLS, only the 5 defaults + meta tools are registered."""
    monkeypatch.delenv("SURFSENSE_MCP_ENABLED_TOOLS", raising=False)
    mock_transport(lambda req: json_response({}))

    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        tools = await client.list_tools()

    tool_names = {t.name for t in tools}
    from surfsense_mcp.tools import DEFAULT_TOOLS, META_TOOLS

    assert tool_names == DEFAULT_TOOLS | META_TOOLS


async def test_enable_tools_registers_new_tool(monkeypatch, mock_transport) -> None:
    """enable_tools dynamically registers a previously disabled tool."""
    monkeypatch.delenv("SURFSENSE_MCP_ENABLED_TOOLS", raising=False)
    mock_transport(lambda req: json_response({"id": 7, "name": "research"}))

    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        # get_search_space should not be available yet.
        tools_before = {t.name for t in await client.list_tools()}
        assert "get_search_space" not in tools_before

        # Enable it.
        result = await client.call_tool("enable_tools", {"tool_names": ["get_search_space"]})
        enable_data = json.loads(result.content[0].text)
        assert enable_data["newly_enabled"] == ["get_search_space"]

        # Now it should be callable.
        result = await client.call_tool("get_search_space", {"search_space_id": 7})
        data = json.loads(result.content[0].text)
        assert data["id"] == 7


async def test_enable_tools_already_enabled(mock_transport) -> None:
    """enable_tools reports already-enabled tools without re-registering."""
    mock_transport(lambda req: json_response({}))
    data = await _call_tool("enable_tools", {"tool_names": ["list_search_spaces", "nonexistent_tool"]})
    assert "list_search_spaces" in data["already_enabled"]
    assert "nonexistent_tool" in data["unknown"]
    assert data["newly_enabled"] == []


async def test_list_available_tools_marks_enabled_correctly(monkeypatch, mock_transport) -> None:
    """list_available_tools reflects which tools are enabled vs disabled."""
    monkeypatch.delenv("SURFSENSE_MCP_ENABLED_TOOLS", raising=False)
    mock_transport(lambda req: json_response({}))

    mcp = get_stdio_mcp()
    async with Client(mcp) as client:
        result = await client.call_tool("list_available_tools", {})
        data = json.loads(result.content[0].text)

    assert data["enabled_count"] == 5
    assert data["total_tools"] == 28

    # Verify the default tools are marked enabled.
    from surfsense_mcp.tools import DEFAULT_TOOLS

    for cat_tools in data["categories"].values():
        for tool in cat_tools:
            if tool["name"] in DEFAULT_TOOLS:
                assert tool["enabled"] is True
            else:
                assert tool["enabled"] is False
