"""Bounded normalized content carried out of one MCP tool result.

The public bridge remains string-compatible for existing tool settlement while
retaining validated image blocks long enough for the task media boundary to
persist them. Raw base64 never enters a Turn projection or tool metadata.
"""

from __future__ import annotations

from typing import TypedDict

MAX_MCP_IMAGES_PER_TURN = 20
MAX_MCP_IMAGE_BYTES = 8 * 1024 * 1024
MAX_MCP_IMAGE_TOTAL_BYTES = 40 * 1024 * 1024

_ALLOWED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
}


class MCPImageContent(TypedDict):
    data: str
    mimeType: str


class MCPToolResult(str):
    """A normal tool-result string with bounded out-of-band MCP images."""

    image_contents: tuple[MCPImageContent, ...]

    def __new__(
        cls,
        text: str,
        *,
        image_contents: tuple[MCPImageContent, ...] = (),
    ) -> "MCPToolResult":
        value = super().__new__(cls, text)
        value.image_contents = tuple(image_contents[:MAX_MCP_IMAGES_PER_TURN])
        return value


def extract_mcp_image_contents(result: object) -> tuple[MCPImageContent, ...]:
    """Copy supported MCP ImageContent references without decoding base64."""
    images: list[MCPImageContent] = []
    for block in getattr(result, "content", ()) or ():
        if str(getattr(block, "type", "")).lower() != "image":
            continue
        data = getattr(block, "data", None)
        mime_type = (
            getattr(block, "mimeType", None)
            or getattr(block, "mime_type", None)
        )
        if not isinstance(data, str) or not data:
            continue
        normalized_mime = str(mime_type or "").lower().strip()
        if normalized_mime == "image/jpg":
            normalized_mime = "image/jpeg"
        if normalized_mime not in _ALLOWED_IMAGE_MIME_TYPES:
            continue
        images.append({"data": data, "mimeType": normalized_mime})
        if len(images) >= MAX_MCP_IMAGES_PER_TURN:
            break
    return tuple(images)


__all__ = [
    "MAX_MCP_IMAGES_PER_TURN",
    "MAX_MCP_IMAGE_BYTES",
    "MAX_MCP_IMAGE_TOTAL_BYTES",
    "MCPImageContent",
    "MCPToolResult",
    "extract_mcp_image_contents",
]
