"""Diagnostics observe Conversation Sync v3 without fetching a transcript."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import (
    runtime_section,
    runtime_section_names,
    runtime_section_path,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = Path(runtime_section_path("diag_collect.js"))


def test_diag_collector_is_ordered_before_application_boot():
    names = runtime_section_names()
    assert names.index("diag_collect.js") < names.index("main.js")


def _run(mode: str) -> dict:
    if not shutil.which("node"):
        pytest.skip("node is required")
    harness = r"""
const fs = require('fs');
const mode = process.argv[2];
global.window = globalThis;
global.runtimeScope = globalThis;
global.location = {href:'https://host/proxy/15000/'};
global.navigator = {userAgent:'Tofu diagnostics test'};
global.activeConvId = 'conv-a';
global.conversations = [{
  id:'conv-a', _turnSnapshotRequired:false, _serverTurnCount:2,
}];
const surface = {
  dataset:{conversationId:'conv-a', conversationRevision:'12', transport:'live'},
  querySelectorAll:() => [{}, {}],
};
global.document = {
  documentElement:{style:{getPropertyValue:() => '812px'}},
  querySelector:(selector) => selector.includes('conversation-surface')
    ? surface : {getAttribute:() => 'assets/main.js'},
};
global.runtimeScope.ConversationTurnRead = {
  state:() => ({conversationRevision:12, transport:'live',
    livePhase:{phase:'working'}}),
  ordered:() => [{turnId:'t1'}, {turnId:'t2'}],
  activeAttemptIds:() => ['attempt-a'],
};
global.runtimeScope.__tofuDiagRing = ['warn-a', 'warn-b'];
global.Api = new Proxy({}, {
  get() { throw new Error('diagnostics must not issue an API request'); },
});
if (mode === 'modern') {
  global.TofuModules = {
    collectDiagnostics:() => Promise.resolve(JSON.stringify({source:'vite'})),
  };
} else if (mode === 'reject') {
  global.TofuModules = {
    collectDiagnostics:() => Promise.reject(new Error('chunk unavailable')),
  };
}
eval(fs.readFileSync(process.argv[1], 'utf8'));
Promise.resolve(global.runtimeScope.__tofuCollectDiagnostics()).then((value) => {
  const parsed = JSON.parse(value);
  console.log(JSON.stringify({
    source:parsed.source || null,
    keys:Object.keys(parsed).sort(),
    sync:parsed.conversationSync || null,
    probe:parsed.liveStateProbe || null,
    active:parsed.activeConv || null,
    surface:parsed.surface || null,
    recentLogLength:(parsed.recentLog || []).length,
  }));
}).catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        [shutil.which("node"), "-e", harness, str(COLLECTOR), mode],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_fallback_collector_reports_store_and_surface_state_only():
    result = _run("fallback")
    assert result["sync"] == {
        "protocol": "conversation-sync-v3",
        "authority": "sidecar-turn-store",
        "browserTranscriptCache": "none",
    }
    assert result["probe"] == {
        "protocol": "conversation-sync-v3",
        "conversationId": "conv-a",
        "revision": 12,
        "transport": "live",
        "turnCount": 2,
        "activeAttemptCount": 1,
        "livePhase": {"phase": "working"},
    }
    assert result["active"]["inMemoryTurnCount"] == 2
    assert result["active"]["serverTurnCount"] == 2
    assert result["surface"] == {
        "conversationId": "conv-a",
        "revision": 12,
        "transport": "live",
        "turnNodeCount": 2,
    }
    assert result["recentLogLength"] == 2
    assert "liveGetProbe" not in result["keys"]
    assert "windowConfig" not in result["keys"]


def test_collector_prefers_typed_chunk_and_falls_back_if_it_rejects():
    assert _run("modern")["source"] == "vite"
    fallback = _run("reject")
    assert fallback["source"] is None
    assert fallback["probe"]["revision"] == 12


def test_diagnostics_sources_cannot_reintroduce_message_window_probe():
    retained = runtime_section("diag_collect.js")
    typed = (ROOT / "frontend/src/features/diagnostics.ts").read_text(
        encoding="utf-8",
    )
    for retired in (
        "convWindowParam", "liveGetProbe", "windowConfig",
        "messagesReturned", "parsed.messages", "getResponse(",
    ):
        assert retired not in retained
        assert retired not in typed
