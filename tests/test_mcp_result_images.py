"""MCP ImageContent becomes durable, bounded Turn media without base64 leaks."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

from lib.mcp.client._bridge import MCPBridge
from lib.mcp.result_content import MCPToolResult
from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
from lib.turn_lifecycle import _task_projection

pytestmark = pytest.mark.unit


class _Session:
    def __init__(self, result):
        self.result = result

    async def call_tool(self, tool_name, arguments=None, read_timeout_seconds=None):
        return self.result


def _bridge_call(result):
    bridge = MCPBridge()
    handle = SimpleNamespace(name="server", sdk_generation=0, session=_Session(result))
    return asyncio.run(bridge._async_call_tool(handle, "read", {}, None))


def test_bridge_keeps_text_compatible_and_carries_image_out_of_band():
    image = SimpleNamespace(
        type="image", data=base64.b64encode(b"png-bytes").decode(), mimeType="image/png"
    )
    text = SimpleNamespace(type="text", text="document body")

    result = _bridge_call(SimpleNamespace(content=[text, image], isError=False))

    assert isinstance(result, str)
    assert isinstance(result, MCPToolResult)
    assert "document body" in result
    assert result.image_contents == ({"data": image.data, "mimeType": "image/png"},)
    assert image.data not in str(result)


def test_mcp_adapter_captures_image_before_finalizing_tool_round(monkeypatch):
    from lib.tasks_pkg.handlers import _adapter

    result = MCPToolResult(
        "tool text",
        image_contents=({"data": "aW1hZ2U=", "mimeType": "image/png"},),
    )
    captured = []
    finalized = []
    monkeypatch.setattr(
        "lib.tasks_pkg.mcp_result_media.capture_mcp_result_images",
        lambda task, value, **kwargs: captured.append((task, value, kwargs)) or 1,
    )
    monkeypatch.setattr(_adapter, "_build_simple_meta", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        _adapter,
        "_finalize_tool_round",
        lambda task, rn, round_entry, results: finalized.append(results),
    )
    task = {"id": "task-1"}

    settled = _adapter.simple_call(
        task,
        "mcp__docs__read",
        {},
        0,
        {},
        "call-1",
        executor=lambda *_args, **_kwargs: result,
        source="MCP",
    )

    assert settled == ("call-1", result, False)
    assert captured == [
        (
            task,
            result,
            {"source_tool": "mcp__docs__read", "tool_call_id": "call-1"},
        )
    ]
    assert finalized == [[{}]]


def test_capture_persists_refs_not_base64_and_projects_authoritative_images(monkeypatch):
    encoded = base64.b64encode(b"valid image payload").decode()
    result = MCPToolResult(
        "[image content]",
        image_contents=({"data": encoded, "mimeType": "image/png"},),
    )
    calls = []

    def ingest(raw, mime_type, **kwargs):
        calls.append((raw, mime_type, kwargs))
        return {
            "attachmentId": "doc-1",
            "preview": "/api/v1/media/attachments/doc-1/source",
            "caption": "Image from mcp__docs__read",
            "sizeKB": 1,
            "mimeType": "image/png",
            "sourceTool": "mcp__docs__read",
            "toolCallId": "call-1",
        }

    monkeypatch.setattr("lib.media_attachments.ingest_mcp_image", ingest)
    task = {
        "id": "task-1",
        "_userId": 7,
        "_mcpImages": [],
        "_mcpImageBytes": 0,
        "config": {},
        "content": "answer",
        "thinking": "",
        "toolRounds": [],
    }

    assert capture_mcp_result_images(
        task, result, source_tool="mcp__docs__read", tool_call_id="call-1"
    ) == 1
    projection = _task_projection(task, {})

    assert calls[0][0] == b"valid image payload"
    assert projection["images"][0]["attachmentId"] == "doc-1"
    assert encoded not in repr(projection)


def test_capture_rejects_invalid_base64_without_failing_tool(monkeypatch):
    monkeypatch.setattr(
        "lib.media_attachments.ingest_mcp_image",
        lambda *args, **kwargs: pytest.fail("invalid base64 must not reach storage"),
    )
    result = MCPToolResult(
        "tool text survives",
        image_contents=({"data": "not base64!", "mimeType": "image/png"},),
    )
    task = {"_userId": 3, "_mcpImages": [], "_mcpImageBytes": 0}

    assert capture_mcp_result_images(
        task, result, source_tool="mcp__x__read", tool_call_id="call-bad"
    ) == 0
    assert str(result) == "tool text survives"
    assert task["_mcpImages"] == []


def test_ingest_mcp_image_uses_owner_scoped_attachment_authority(monkeypatch):
    seen = {"add": [], "patch": []}

    def add_document(raw, filename, **kwargs):
        seen["add"].append((raw, filename, kwargs))
        return {"id": "image-doc", "name": filename, "kind": ".png"}

    def patch_media_metadata(document_id, updates, **kwargs):
        seen["patch"].append((document_id, updates, kwargs))
        return None

    monkeypatch.setattr("lib.knowledge.add_document", add_document)
    monkeypatch.setattr("lib.knowledge.patch_media_metadata", patch_media_metadata)

    from lib.media_attachments import ingest_mcp_image

    image = ingest_mcp_image(
        b"fake png",
        "image/png",
        user_id=11,
        source_tool="mcp__docs__read",
        tool_call_id="call-9",
        ordinal=0,
    )

    second = ingest_mcp_image(
        b"different fake png",
        "image/png",
        user_id=11,
        source_tool="mcp__docs__read",
        tool_call_id="call-9",
        ordinal=0,
    )

    assert seen["add"][0][2]["user_id"] == 11
    assert seen["add"][0][2]["scope"] == "attachment"
    assert seen["add"][0][2]["command_id"] != seen["add"][1][2]["command_id"]
    assert seen["patch"][0][1]["origin"] == "mcp_tool_result"
    assert image["preview"] == "/api/v1/media/attachments/image-doc/source"
    assert image["sourceTool"] == "mcp__docs__read"
    assert second["sourceTool"] == "mcp__docs__read"
