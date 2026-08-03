"""Document tools for the SurfSense MCP Server."""

import base64
import binascii
import mimetypes
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from surfsense_mcp.client import authed_multipart_post, authed_request
from surfsense_mcp.config import delete_tools_enabled

# Mirrors MAX_FILE_SIZE_BYTES in surfsense_backend/app/routes/documents_routes.py.
# Duplicated rather than imported because the backend is a separate package.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024


def register_document_tools(mcp: FastMCP) -> None:
    """Register document tools."""

    @mcp.tool()
    async def list_documents(
        search_space_id: int | None = None,
        document_types: str | None = None,
        page: int = 0,
        page_size: int = 50,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        """
        List documents in a search space with pagination and type filter.

        Args:
            search_space_id: Restrict to one search space.
            document_types: Comma-separated type filter (e.g.
                "EXTENSION,FILE,SLACK_CONNECTOR").
            page: 0-based page number. Defaults to 0.
            page_size: Results per page (1-100).
            sort_by: One of "created_at", "title", "document_type". The
                backend does not support sorting by updated_at.
            sort_order: "asc" or "desc".

        Returns:
            Paginated response: items, total, page, page_size, has_next,
            has_prev.
        """
        params: dict[str, Any] = {
            "page": page,
            "page_size": max(1, min(page_size, 100)),
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
        if document_types:
            params["document_types"] = document_types
        if search_space_id is not None:
            params["search_space_id"] = search_space_id
        response = await authed_request("GET", "/api/v1/documents", params=params)
        return response.json()

    @mcp.tool()
    async def search_documents(
        title: str,
        search_space_id: int | None = None,
        document_types: str | None = None,
        page: int = 0,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """
        Search documents by title substring (case-insensitive keyword match).

        This is NOT semantic search — it performs a simple ILIKE on the
        document title column. Use it to find documents by known title
        fragments, not to explore topics.

        Args:
            title: Title substring to search for (required).
            search_space_id: Restrict results to a single search space. If
                omitted, searches across all spaces the user can access.
            document_types: Comma-separated document type filter (e.g.
                "EXTENSION,FILE,SLACK_CONNECTOR").
            page: 0-based page number. Defaults to 0.
            page_size: Results per page (1-100). Defaults to 20.

        Returns:
            Paginated response with keys: items (list of documents), total,
            page, page_size, has_next, has_prev.
        """
        params: dict[str, Any] = {"title": title, "page": page, "page_size": page_size}
        if search_space_id is not None:
            params["search_space_id"] = search_space_id
        if document_types:
            params["document_types"] = document_types
        response = await authed_request("GET", "/api/v1/documents/search", params=params)
        return response.json()

    @mcp.tool()
    async def get_document(document_id: int) -> dict[str, Any]:
        """
        Retrieve a single document by its ID, including content and metadata.

        Args:
            document_id: The integer document ID (as returned by
                search_documents or get_recent_documents).

        Returns:
            A document object containing at least: id, title, document_type,
            document_metadata, content, content_hash, search_space_id,
            folder_id, created_at, updated_at, status.
        """
        response = await authed_request("GET", f"/api/v1/documents/{document_id}")
        return response.json()

    @mcp.tool()
    async def get_recent_documents(
        search_space_id: int | None = None,
        document_types: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        List recently created documents, newest first.

        Thin wrapper over the list-documents endpoint with sort_by=created_at
        and sort_order=desc enforced. The SurfSense backend does not support
        sorting by updated_at, so "recent" here means recently created.

        Args:
            search_space_id: Restrict to one search space. Omit to list across
                all accessible spaces.
            document_types: Comma-separated document type filter.
            limit: Maximum number of documents to return (1-100). Defaults to 20.

        Returns:
            Paginated response with items, total, page, page_size, has_next,
            has_prev.
        """
        params: dict[str, Any] = {
            "page": 0,
            "page_size": max(1, min(limit, 100)),
            "sort_by": "created_at",
            "sort_order": "desc",
        }
        if search_space_id is not None:
            params["search_space_id"] = search_space_id
        if document_types:
            params["document_types"] = document_types
        response = await authed_request("GET", "/api/v1/documents", params=params)
        return response.json()

    @mcp.tool()
    async def upload_document(
        file_path: str,
        search_space_id: int,
        should_summarize: bool = True,
    ) -> dict[str, Any]:
        """
        Upload a local file to a search space for indexing.

        Prefer this over `upload_document_content` whenever the MCP server has
        filesystem access to the file (stdio mode on the user's machine, or a
        shared mount). The bytes go straight to disk → multipart upload, with
        no base64 inflation across the MCP transport.

        Args:
            file_path: Absolute path to a local file (PDF, DOCX, XLSX, CSV,
                TXT, MD, ...). Must be readable by the MCP server process and
                no larger than 500 MB.
            search_space_id: Space that will own the new document(s).
            should_summarize: If true, the backend generates an LLM summary
                once processing completes.

        Returns:
            The raw JSON response from `/api/v1/documents/fileupload`,
            typically including `document_ids` and a status/message string.
        """
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"File not found or not a regular file: {file_path}")

        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"File '{path.name}' is {size / (1024 * 1024):.1f} MB, "
                f"exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB per-file limit."
            )

        mime_type, _ = mimetypes.guess_type(str(path))
        mime_type = mime_type or "application/octet-stream"

        # Stream from disk rather than read_bytes() — uploads can be up to
        # 500 MB, and HTTP-mode runs in a container where buffering the
        # whole payload in RAM risks OOM under concurrent uploads.
        with path.open("rb") as file_obj:
            response = await authed_multipart_post(
                "/api/v1/documents/fileupload",
                files={"files": (path.name, file_obj, mime_type)},
                data={
                    "search_space_id": str(search_space_id),
                    "should_summarize": str(should_summarize).lower(),
                },
                timeout=300.0,
            )
        return response.json()

    @mcp.tool()
    async def upload_document_content(
        filename: str,
        content_base64: str,
        search_space_id: int,
        mime_type: str | None = None,
        should_summarize: bool = True,
    ) -> dict[str, Any]:
        """
        Upload a file to a search space by passing its bytes inline (base64).

        Use this when the caller cannot share a filesystem path with the MCP
        server — e.g. a Claude Desktop chat attachment, or a hosted HTTP-mode
        MCP. MCP is JSON-RPC, so binary bytes have to ride as base64; that
        inflates payload by ~33% (a 100 MB PDF becomes ~133 MB on the wire).
        When the MCP server can read the file directly, prefer
        `upload_document` instead. Hard limit: 500 MB raw.

        Args:
            filename: File name including extension (e.g. "quarterly.pdf").
                Used by SurfSense to pick the parser and set the MIME type when
                `mime_type` is not given.
            content_base64: Base64-encoded file bytes. Data URL prefixes like
                "data:application/pdf;base64," are stripped automatically.
            search_space_id: Space that will own the new document.
            mime_type: Optional explicit MIME type. If omitted, it is guessed
                from `filename` and falls back to "application/octet-stream".
            should_summarize: If true, the backend generates an LLM summary
                once processing completes.

        Returns:
            The raw JSON response from `/api/v1/documents/fileupload`,
            typically including `document_ids` and a status/message string.
        """
        if not filename or not filename.strip():
            raise ValueError("filename is required")

        payload = content_base64.strip()
        if payload.startswith("data:"):
            _, _, payload = payload.partition(",")
        payload = "".join(payload.split())
        if not payload:
            raise ValueError("content_base64 is empty")

        try:
            file_bytes = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"content_base64 is not valid base64: {exc}") from exc
        if not file_bytes:
            raise ValueError("content_base64 decoded to zero bytes")

        if len(file_bytes) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"File '{filename}' is {len(file_bytes) / (1024 * 1024):.1f} MB, "
                f"exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB per-file limit."
            )

        resolved_mime = mime_type
        if not resolved_mime:
            guessed, _ = mimetypes.guess_type(filename)
            resolved_mime = guessed or "application/octet-stream"

        response = await authed_multipart_post(
            "/api/v1/documents/fileupload",
            files={"files": (filename, file_bytes, resolved_mime)},
            data={
                "search_space_id": str(search_space_id),
                "should_summarize": str(should_summarize).lower(),
            },
            timeout=300.0,
        )
        return response.json()

    @mcp.tool()
    async def update_document(
        document_id: int,
        document_type: str,
        content: str,
        search_space_id: int,
    ) -> dict[str, Any]:
        """
        Replace the content of a document.

        The SurfSense backend's DocumentUpdate schema requires all three
        fields (`document_type`, `content`, `search_space_id`) to be present
        — there is no partial-patch path in this fork. To rename a document
        or tweak metadata, you currently cannot use this endpoint.

        Args:
            document_id: Document to update.
            document_type: Must match one of the DocumentType enum values
                (e.g. "NOTE", "FILE", "EXTENSION", "CRAWLED_URL", ...). Pass
                the document's existing type unless you are intentionally
                reclassifying it.
            content: New content. For note-type documents this is markdown;
                for other types the backend treats it as raw text.
            search_space_id: Search space the document belongs to. Pass the
                existing value unless you are intentionally moving it.

        Returns:
            The updated DocumentRead object.
        """
        body = {
            "document_type": document_type,
            "content": content,
            "search_space_id": search_space_id,
        }
        response = await authed_request("PUT", f"/api/v1/documents/{document_id}", json=body)
        return response.json()

    if delete_tools_enabled():

        @mcp.tool()
        async def delete_document(document_id: int) -> dict[str, Any]:
            """
            Permanently delete a document.

            Requires DOCUMENTS_DELETE permission. Documents in "processing" state
            cannot be deleted.

            Returns:
                Backend confirmation: `{"message": "..."}`.
            """
            response = await authed_request("DELETE", f"/api/v1/documents/{document_id}")
            return response.json()

    @mcp.tool()
    async def get_document_status(search_space_id: int, document_ids: str) -> dict[str, Any]:
        """
        Batch-check ETL status for a set of documents in a search space.

        Useful for polling after an upload — the backend processes files
        asynchronously via Celery.

        Args:
            search_space_id: Search space containing the documents.
            document_ids: Comma-separated document IDs (e.g. "12,13,14").

        Returns:
            `{"items": [{"id", "title", "document_type", "status": {"state", "reason"}}, ...]}`.
            `state` is one of "pending", "processing", "ready", "failed".
        """
        response = await authed_request(
            "GET",
            "/api/v1/documents/status",
            params={
                "search_space_id": search_space_id,
                "document_ids": document_ids,
            },
        )
        return response.json()

    @mcp.tool()
    async def get_document_type_counts(
        search_space_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Get document counts grouped by DocumentType.

        Args:
            search_space_id: If provided, restrict counts to one space. If
                omitted, aggregates across all spaces the user can access.

        Returns:
            Raw backend response, typically a mapping of type-name → count.
        """
        params: dict[str, Any] = {}
        if search_space_id is not None:
            params["search_space_id"] = search_space_id
        response = await authed_request("GET", "/api/v1/documents/type-counts", params=params)
        return response.json()
