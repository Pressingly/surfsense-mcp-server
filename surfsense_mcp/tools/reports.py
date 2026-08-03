"""Report tools for the SurfSense MCP Server."""

from typing import Any

from fastmcp import FastMCP

from surfsense_mcp.client import (
    authed_request,
    stream_authed_get,
)
from surfsense_mcp.config import delete_tools_enabled

VALID_EXPORT_FORMATS = {"pdf", "docx", "html", "latex", "epub", "odt", "plain"}


def register_report_tools(mcp: FastMCP) -> None:
    """Register report tools."""

    @mcp.tool()
    async def list_reports(
        search_space_id: int | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        List reports the authenticated user has access to.

        Args:
            search_space_id: If provided, restrict to one search space. If
                omitted, aggregates across every space the user is a member of.
            skip: Pagination offset.
            limit: Max items to return (1-500 per backend cap).

        Returns:
            List of ReportRead objects: id, title, content_type,
            report_group_id, report_metadata, created_at, updated_at.
        """
        params: dict[str, Any] = {"skip": skip, "limit": limit}
        if search_space_id is not None:
            params["search_space_id"] = search_space_id
        response = await authed_request("GET", "/api/v1/reports", params=params)
        return response.json()

    @mcp.tool()
    async def get_report(report_id: int) -> dict[str, Any]:
        """
        Get a single report's metadata (no content body).

        Use `get_report_content` to retrieve the Markdown body.
        """
        response = await authed_request("GET", f"/api/v1/reports/{report_id}")
        return response.json()

    @mcp.tool()
    async def get_report_content(report_id: int) -> dict[str, Any]:
        """
        Get the full Markdown (or Typst) content of a report, plus version
        siblings from the same report_group_id.

        Returns:
            ReportContentRead: id, title, content, content_type (usually
            "markdown" or "typst"), report_metadata, report_group_id,
            versions: list[{id, created_at}].
        """
        response = await authed_request("GET", f"/api/v1/reports/{report_id}/content")
        return response.json()

    @mcp.tool()
    async def export_report(report_id: int, format: str = "pdf") -> dict[str, Any]:
        """
        Export a report in the requested format.

        The backend streams binary bytes for every format (PDF/DOCX/EPUB/ODT)
        and text for HTML/LaTeX/plain. To keep the MCP payload reasonable,
        this tool does NOT return the bytes — it returns content-type,
        content-length, and the filename suggested by the Content-Disposition
        header so the caller can fetch the file out-of-band.

        Args:
            report_id: Report to export.
            format: One of "pdf", "docx", "html", "latex", "epub", "odt",
                "plain". Typst-backed reports only support "pdf".

        Returns:
            `{"report_id": int, "format": str, "content_type": str,
              "size_bytes": int, "filename": str | None}`.
        """
        fmt = format.lower()
        if fmt not in VALID_EXPORT_FORMATS:
            raise ValueError(f"Unsupported export format '{format}'. Valid options: {sorted(VALID_EXPORT_FORMATS)}")

        # Stream via the shared helper — exports can be many MB and we only
        # need headers + size. stream_authed_get() also gives us the
        # 401-retry-once behavior on stdio password mode (without it, exports
        # would be the only tool that fails on a stale cached token).
        async with stream_authed_get(
            f"/api/v1/reports/{report_id}/export",
            params={"format": fmt},
        ) as response:
            content_type = response.headers.get("content-type", "application/octet-stream")
            disposition = response.headers.get("content-disposition") or ""
            filename: str | None = None
            if "filename=" in disposition:
                filename = disposition.split("filename=", 1)[1].strip().strip('"')

            size_bytes: int | None = None
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    size_bytes = int(content_length)
                except ValueError:
                    size_bytes = None
            if size_bytes is None:
                size_bytes = 0
                async for chunk in response.aiter_bytes():
                    size_bytes += len(chunk)

        return {
            "report_id": report_id,
            "format": fmt,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "filename": filename,
        }

    if delete_tools_enabled():

        @mcp.tool()
        async def delete_report(report_id: int) -> dict[str, Any]:
            """
            Permanently delete a report.

            Returns:
                Backend confirmation: `{"message": "..."}`.
            """
            response = await authed_request("DELETE", f"/api/v1/reports/{report_id}")
            return response.json()
