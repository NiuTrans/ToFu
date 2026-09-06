"""tests/test_wire_fixtures_frontend.py — Frontend golden-wire conformance.

The consumer half of the ``contracts/fixtures/`` corpus: the frontend must
accept every fixture through the SAME authorities production uses.

  A. **Generated mirror** — every event/push fixture's fields are a subset
     of the interface the TS mirror declares
     (``event-contract.generated.ts``); a fixture the mirror cannot type is
     a contract drift, not a test update.
  B. **Reducer conformance (node)** — the real owners bundled by the real
     test bundler: ``decodeConversationSyncSnapshot`` (generated fail-closed
     decoder) accepts the modern snapshot fixture and rejects the legacy
     pre-``attemptId`` shape; ``reduceTurnState`` projects snapshot and
     turn-page fixtures into turn/lane/history blocks; every push fixture
     passes the ``frame-identity`` narrowing guard.

If this fails
-------------
The frontend contract surface drifted from the corpus. Regenerate the
mirror (``scripts/gen_event_contract.py``) or the corpus
(``scripts/gen_wire_fixtures.py``) — never edit either artifact by hand.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts" / "fixtures"
GENERATED = ROOT / "frontend" / "src" / "api" / "event-contract.generated.ts"


def _fixture_paths(subdir: str) -> list[Path]:
    return sorted((FIXTURES / subdir).glob("*.json"))


# ═══════════════════════════════════════════════════════════════════
#  A. Generated TS mirror
# ═══════════════════════════════════════════════════════════════════


def _generated_interfaces() -> dict[str, set[str]]:
    source = GENERATED.read_text(encoding="utf-8")
    interfaces = {}
    for match in re.finditer(r"export interface (\w+) \{(.*?)\n\}", source, re.S):
        name, body = match.groups()
        interfaces[name] = set(re.findall(r"^  (\w+)\??:", body, re.M))
    return interfaces


def _event_interface_name(event_type: str) -> str:
    # Mirrors scripts/gen_event_contract.py:_interface_name.
    return "".join(part.capitalize() or "_" for part in event_type.split("_")) + "Event"


def _push_interface_name(frame_type: str) -> str:
    # Mirrors scripts/gen_event_contract.py:_push_interface_name.
    return (
        "".join(part.capitalize() or "_" for part in re.split(r"[_\.]", frame_type))
        + "PushFrame"
    )


def test_event_fixtures_stay_inside_the_generated_mirror():
    interfaces = _generated_interfaces()
    paths = _fixture_paths("events")
    assert paths
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        declared = interfaces.get(_event_interface_name(path.stem))
        assert declared is not None, f"{path.name}: mirror interface missing"
        extras = set(payload) - {"type"} - declared
        assert not extras, f"{path.name}: undeclared mirror fields {extras}"


def test_push_fixtures_stay_inside_the_generated_mirror():
    interfaces = _generated_interfaces()
    paths = _fixture_paths("push")
    assert paths
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        declared = interfaces.get(_push_interface_name(path.stem))
        assert declared is not None, f"{path.name}: mirror interface missing"
        extras = set(payload) - {"type"} - declared
        assert not extras, f"{path.name}: undeclared mirror fields {extras}"


# ═══════════════════════════════════════════════════════════════════
#  B. Reducer conformance — real owners under node
# ═══════════════════════════════════════════════════════════════════


def _bundle_harness(tmp_path: Path) -> str:
    from tests._runtime_sections import native_module_path

    entry = tmp_path / "wire-fixtures-harness.ts"
    entry.write_text(
        "export { createTurnState, reduceTurnState } from "
        f"{(ROOT / 'frontend/src/core/turn-state.ts').as_posix()!r};\n"
        "export { decodeConversationSyncSnapshot } from "
        f"{(ROOT / 'frontend/src/api/conversation-sync.generated.ts').as_posix()!r};\n"
        "export { isContractedPushFrame } from "
        f"{(ROOT / 'frontend/src/core/frame-identity.ts').as_posix()!r};\n",
        encoding="utf-8",
    )
    return native_module_path("wire-fixtures/harness.js", entry)


def _run_node(script: str, *paths) -> dict:
    result = subprocess.run(
        ["node", "-e", script, *(str(path) for path in paths)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_reducers_accept_every_fixture(tmp_path):
    bundle = _bundle_harness(tmp_path)
    snapshot_path = FIXTURES / "sync_v3" / "ConversationSyncSnapshot.json"
    legacy_path = (
        FIXTURES
        / "sync_v3"
        / "ConversationSyncSnapshot.legacy-observation-attempt-id.json"
    )
    page_path = FIXTURES / "sync_v3" / "ConversationTurnPage.json"
    push_paths = _fixture_paths("push")
    output = _run_node(
        r"""
const fs = require('fs');
// Browser-platform bundle under bare node: the owners only touch window
// for optional hooks, so the global scope stands in for it.
globalThis.window = globalThis;
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const read = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const snapshot = read(process.argv[2]);
const legacy = read(process.argv[3]);
const page = read(process.argv[4]);
const pushes = process.argv.slice(5).map(read);
const out = {};

// Fail-closed generated decoder: modern fixture accepted, legacy rejected.
const decoded = decodeConversationSyncSnapshot(snapshot);
out.decoderReturnsFixtureConversation =
  decoded.conversationId === snapshot.conversationId;
try {
  decodeConversationSyncSnapshot(legacy);
  out.legacyRejected = false;
} catch (_error) {
  out.legacyRejected = true;
}

// Snapshot reduction: every fixture turn projects into the store.
let state = createTurnState(snapshot.conversationId);
state = reduceTurnState(state, { type: 'snapshot', snapshot: decoded });
const firstTurn = decoded.turns[0];
const projected = state.turnsById[firstTurn.turnId];
out.turnsProjected =
  Object.keys(state.turnsById).length === decoded.turns.length;
out.projectionContentPreserved =
  !!projected && !!projected.projection
  && projected.projection.content === firstTurn.projection.content;
out.laneOrderTracksTurn =
  (state.laneOrder[firstTurn.laneId] || []).includes(firstTurn.turnId);
out.turnWindowRecorded =
  !!state.historyByLane[decoded.turnWindow.laneId]
  && state.historyByLane[decoded.turnWindow.laneId].totalTurns
     === decoded.turnWindow.totalTurns;
out.queueItemsAccepted =
  state.queueItems.length === decoded.queueItems.length;

// History-page reduction lands on the same store.
state = reduceTurnState(state, { type: 'snapshot', snapshot: page });
out.pageTurnsProjected = page.turns.every(
  (turn) => !!state.turnsById[turn.turnId]);
out.pageHistoryRecorded =
  !!state.historyByLane[page.laneId]
  && state.historyByLane[page.laneId].hasMore === page.hasMore;

// Every declared push frame narrows through the identity guard.
out.pushFramesAccepted =
  pushes.length > 0 && pushes.every((frame) => isContractedPushFrame(frame));

console.log(JSON.stringify(out));
""",
        Path(bundle),
        snapshot_path,
        legacy_path,
        page_path,
        *push_paths,
    )
    assert output == {
        "decoderReturnsFixtureConversation": True,
        "legacyRejected": True,
        "turnsProjected": True,
        "projectionContentPreserved": True,
        "laneOrderTracksTurn": True,
        "turnWindowRecorded": True,
        "queueItemsAccepted": True,
        "pageTurnsProjected": True,
        "pageHistoryRecorded": True,
        "pushFramesAccepted": True,
    }
