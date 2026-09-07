"""Projected MCP image refs use the native attachment renderer and preview action."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLER = ROOT / "scripts" / "vite_test_bundle.mjs"
ENTRY = ROOT / "frontend/src/conversation/index.ts"


def _ready() -> bool:
    if not shutil.which("node") or not BUNDLER.is_file():
        return False
    return subprocess.run(
        [shutil.which("node"), "-e", "require('jsdom')"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).returncode == 0


pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not _ready(), reason="node + jsdom + Vite test bundler required"),
]


@pytest.fixture(scope="module")
def conversation_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    built = tmp_path_factory.mktemp("mcp-image-projection") / "conversation.cjs"
    result = subprocess.run(
        [
            str(BUNDLER),
            str(ENTRY),
            "--bundle",
            "--format=cjs",
            "--platform=node",
            f"--outfile={built}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return built


def test_mcp_turn_image_renders_as_interactive_thumbnail(
    conversation_bundle: Path,
) -> None:
    script = f"""
const feature = require({json.dumps(str(conversation_bundle))});
const {{ JSDOM }} = require('jsdom');
const dom = new JSDOM('<main><div id="block"></div></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const source = {{
  turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projectionRevision:1,
  settlement:{{outcome:'completed'}}, createdAt:1, updatedAt:1,
  projection:{{
    content:'Here is the image',
    images:[{{
      attachmentId:'image-doc',
      preview:'/api/v1/media/attachments/image-doc/source',
      caption:'Image from mcp__docs__read',
      sizeKB:12.5,
      mimeType:'image/png',
      sourceTool:'mcp__docs__read',
      toolCallId:'call-1',
    }}],
  }},
}};
const blocks = feature.selectTurnBlocks(source, 'original');
const attachment = blocks.find(block => block.kind === 'attachments');
const node = document.getElementById('block');
const classic = feature.createClassicConversationRenderers({{
  renderSafeMarkdownHtml:value => value,
}});
classic.renderBlock(node, attachment, {{turn:{{source}}}});
const button = node.querySelector('[data-conversation-action="preview-image"]');
const image = node.querySelector('img');
console.log(JSON.stringify({{
  blockKind: attachment?.kind,
  imageCount: attachment?.images?.length,
  action: button?.dataset.conversationAction,
  operation: button?.dataset.operation,
  src: image?.getAttribute('src'),
  alt: image?.getAttribute('alt'),
}}));
"""
    result = subprocess.run(
        [shutil.which("node"), "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout.strip().splitlines()[-1])
    assert rendered == {
        "blockKind": "attachments",
        "imageCount": 1,
        "action": "preview-image",
        "operation": "0",
        "src": "/api/v1/media/attachments/image-doc/source",
        "alt": "Image from mcp__docs__read",
    }
