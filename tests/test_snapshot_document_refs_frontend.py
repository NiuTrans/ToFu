"""Browser materialization contract for snapshot-only shared tool documents."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.conversation_sync import validation as contract_validation
from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS
from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend/src/conversation/domain/tool-segment-references.ts"


def _run(harness: str) -> dict:
    if not shutil.which("node"):
        pytest.skip("node is required")
    bundle = native_module_path("snapshot-tool-document-refs.js", SOURCE)
    completed = subprocess.run(
        ["node", "-e", harness, bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_shared_documents_restore_exact_round_and_segment_objects():
    result = _run(r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const contentKey = `sha256:${'a'.repeat(64)}`;
const resultsKey = `sha256:${'b'.repeat(64)}`;
const sharedContent = 'shared result';
const sharedResults = [{title: 'source'}];
const documents = {[contentKey]: sharedContent, [resultsKey]: sharedResults};
const makeTurn = (ordinal) => ({
  turnId: `turn-${ordinal}`,
  projection: {
    toolRounds: [{
      toolCallId: `call-${ordinal}`,
      toolName: 'search_tools',
      toolArgs: {query: 'shared'},
      status: 'done',
      _snapshotDocumentRefs: {
        toolContent: contentKey,
        results: resultsKey,
      },
    }],
    segments: [{
      type: 'tool_use',
      id: `call-${ordinal}`,
      name: 'search_tools',
      result: {},
      roundRef: `call-${ordinal}`,
    }],
  },
});
const wire = {
  turns: [makeTurn(1), makeTurn(2)],
  sharedToolDocuments: documents,
};
const materialized = materializeSnapshotReferences(wire);
const firstRound = materialized.turns[0].projection.toolRounds[0];
const secondRound = materialized.turns[1].projection.toolRounds[0];
const firstSegment = materialized.turns[0].projection.segments[0];
console.log(JSON.stringify({
  copiedEnvelope: materialized !== wire,
  dictionaryDiscarded: !Object.hasOwn(materialized, 'sharedToolDocuments'),
  contentIdentity: firstRound.toolContent === documents[contentKey]
    && firstRound.toolContent === secondRound.toolContent,
  resultsIdentity: firstRound.results === documents[resultsKey]
    && firstRound.results === secondRound.results,
  refsDiscarded: !Object.hasOwn(firstRound, '_snapshotDocumentRefs')
    && !Object.hasOwn(secondRound, '_snapshotDocumentRefs'),
  segmentUsesRound: firstSegment._round === firstRound,
  segmentRestored: firstSegment.input === firstRound.toolArgs
    && firstSegment.result.content === sharedContent,
  wireUntouched: Object.hasOwn(
    wire.turns[0].projection.toolRounds[0], '_snapshotDocumentRefs',
  ) && !Object.hasOwn(wire.turns[0].projection.toolRounds[0], 'toolContent'),
}));
""")
    assert result == {
        "copiedEnvelope": True,
        "dictionaryDiscarded": True,
        "contentIdentity": True,
        "resultsIdentity": True,
        "refsDiscarded": True,
        "segmentUsesRound": True,
        "segmentRestored": True,
        "wireUntouched": True,
    }


def test_projection_content_reference_restores_before_state_publication():
    result = _run(r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const sourceText = 'exact long-form answer';
const sourceThinking = 'exact private reasoning';
const wire = {
  turns: [{
    turnId: 'turn-a',
    status: 'completed',
    projection: {
      toolRounds: [{toolCallId: 'call-a', toolName: 'search'}],
      segments: [{
        type: 'thinking', blockId: 'thinking:round-a', text: sourceThinking,
      }, {
        type: 'text', blockId: 'text:terminal', text: sourceText,
      }],
    },
  }],
  snapshotProjectionRefs: {'turn-a': {
    content: 'text:terminal',
    roundThinking: {'call-a': 'thinking:round-a'},
  }},
};
const materialized = materializeSnapshotReferences(wire);
const materializedRound = materialized.turns[0].projection.toolRounds[0];
console.log(JSON.stringify({
  copiedEnvelope: materialized !== wire,
  copiedTurn: materialized.turns[0] !== wire.turns[0],
  copiedProjection: materialized.turns[0].projection !== wire.turns[0].projection,
  exactContent: materialized.turns[0].projection.content === sourceText,
  exactThinking: materializedRound.thinking === sourceThinking,
  copiedRound: materializedRound
    !== wire.turns[0].projection.toolRounds[0],
  dictionaryDiscarded: !Object.hasOwn(materialized, 'snapshotProjectionRefs'),
  wireUntouched: !Object.hasOwn(wire.turns[0].projection, 'content')
    && !Object.hasOwn(wire.turns[0].projection.toolRounds[0], 'thinking')
    && Object.hasOwn(wire, 'snapshotProjectionRefs'),
}));
""")
    assert result == {
        "copiedEnvelope": True,
        "copiedTurn": True,
        "copiedProjection": True,
        "exactContent": True,
        "exactThinking": True,
        "copiedRound": True,
        "dictionaryDiscarded": True,
        "wireUntouched": True,
    }


def test_projection_content_reference_protocol_violations_fail_closed():
    result = _run(r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const base = () => ({
  turns: [{
    turnId: 'turn-a',
    status: 'completed',
    projection: {segments: [{
      type: 'text', blockId: 'text:terminal', text: 'answer',
    }]},
  }],
  snapshotProjectionRefs: {'turn-a': {content: 'text:terminal'}},
});
const failure = (mutate) => {
  const snapshot = base();
  mutate(snapshot);
  try {
    materializeSnapshotReferences(snapshot);
    return '';
  } catch (error) {
    return String(error && error.message || error);
  }
};
const addThinkingReference = (value) => {
  value.turns[0].projection.toolRounds = [{toolCallId: 'call-a'}];
  value.turns[0].projection.segments.unshift({
    type: 'thinking', blockId: 'thinking:round-a', text: 'reasoning',
  });
  value.snapshotProjectionRefs['turn-a'] = {
    roundThinking: {'call-a': 'thinking:round-a'},
  };
};
console.log(JSON.stringify({
  empty: failure((value) => { value.snapshotProjectionRefs = {}; }),
  missingTurn: failure((value) => {
    value.snapshotProjectionRefs = {'turn-b': {content: 'text:terminal'}};
  }),
  activeTurn: failure((value) => { value.turns[0].status = 'running'; }),
  inlineConflict: failure((value) => {
    value.turns[0].projection.content = 'answer';
  }),
  missingSegment: failure((value) => {
    value.snapshotProjectionRefs['turn-a'].content = 'text:missing';
  }),
  duplicateSegment: failure((value) => {
    value.turns[0].projection.segments.push(
      {...value.turns[0].projection.segments[0]},
    );
  }),
  emptyThinking: failure((value) => {
    value.snapshotProjectionRefs['turn-a'] = {roundThinking: {}};
  }),
  missingRound: failure((value) => {
    value.snapshotProjectionRefs['turn-a'] = {
      roundThinking: {'call-a': 'thinking:round-a'},
    };
  }),
  inlineThinking: failure((value) => {
    addThinkingReference(value);
    value.turns[0].projection.toolRounds[0].thinking = 'reasoning';
  }),
  duplicateThinking: failure((value) => {
    addThinkingReference(value);
    value.turns[0].projection.segments.unshift({
      ...value.turns[0].projection.segments[0],
    });
  }),
}));
""")
    assert "invalid projection references" in result["empty"]
    assert "no unique turn" in result["missingTurn"]
    assert "active turn projection" in result["activeTurn"]
    assert "conflicts with inline data" in result["inlineConflict"]
    assert "no unique source" in result["missingSegment"]
    assert "no unique source" in result["duplicateSegment"]
    assert "invalid round thinking references" in result["emptyThinking"]
    assert "no unique source" in result["missingRound"]
    assert "conflicts with inline data" in result["inlineThinking"]
    assert "no unique source" in result["duplicateThinking"]


def test_shared_document_protocol_violations_fail_closed():
    result = _run(r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const contentKey = `sha256:${'a'.repeat(64)}`;
const resultsKey = `sha256:${'b'.repeat(64)}`;
const base = () => ({
  turns: [{projection: {
    toolRounds: [{_snapshotDocumentRefs: {toolContent: contentKey}}],
    segments: [],
  }}],
  sharedToolDocuments: {[contentKey]: 'content', [resultsKey]: {not: 'array'}},
});
const failure = (mutate) => {
  const snapshot = base();
  mutate(snapshot);
  try {
    materializeSnapshotReferences(snapshot);
    return '';
  } catch (error) {
    return String(error && error.message || error);
  }
};
console.log(JSON.stringify({
  missingDictionary: failure((value) => { delete value.sharedToolDocuments; }),
  missingDocument: failure((value) => {
    value.turns[0].projection.toolRounds[0]._snapshotDocumentRefs.toolContent
      = `sha256:${'c'.repeat(64)}`;
  }),
  inlineConflict: failure((value) => {
    value.turns[0].projection.toolRounds[0].toolContent = 'inline';
  }),
  forbiddenField: failure((value) => {
    value.turns[0].projection.toolRounds[0]._snapshotDocumentRefs
      = {assistantContent: contentKey};
  }),
  wrongResultsType: failure((value) => {
    value.turns[0].projection.toolRounds[0]._snapshotDocumentRefs
      = {results: resultsKey};
  }),
}));
""")
    assert "without documents" in result["missingDictionary"]
    assert "missing shared document" in result["missingDocument"]
    assert "conflicts with inline data" in result["inlineConflict"]
    assert "invalid document references" in result["forbiddenField"]
    assert "results reference is not an array" in result["wrongResultsType"]


def test_generated_browser_schema_enforces_reference_shape_and_capacity():
    if not shutil.which("node"):
        pytest.skip("node is required")
    bundle = native_module_path(
        "conversation-sync-contract-validation.js",
        ROOT / "frontend/src/api/conversation-sync.generated.ts",
    )
    harness = r"""
global.window = globalThis;
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const key = `sha256:${'a'.repeat(64)}`;
const rejected = (schema, value) => {
  try {
    assertConversationSyncSchema(schema, value);
    return false;
  } catch (error) {
    return error instanceof ConversationSyncContractError;
  }
};
const oversized = Object.fromEntries(Array.from(
  {length: 257},
  (_value, index) => [`sha256:${index.toString(16).padStart(64, '0')}`, index],
));
const oversizedProjectionRefs = Object.fromEntries(Array.from(
  {length: 4097},
  (_value, index) => [`turn-${index}`, {content: 'text:terminal'}],
));
const oversizedThinkingRefs = Object.fromEntries(Array.from(
  {length: 4097},
  (_value, index) => [`call-${index}`, 'thinking:round'],
));
console.log(JSON.stringify({
  valid: assertConversationSyncSchema(
    'SnapshotDocumentReferences', {toolContent: key},
  ).toolContent === key,
  empty: rejected('SnapshotDocumentReferences', {}),
  badDigest: rejected(
    'SnapshotDocumentReferences', {toolContent: 'sha256:short'},
  ),
  extraField: rejected(
    'SnapshotDocumentReferences', {assistantContent: key},
  ),
  badDictionaryKey: rejected(
    'SnapshotSharedToolDocuments', {'not-a-digest': 'value'},
  ),
  oversized: rejected('SnapshotSharedToolDocuments', oversized),
  numberIsNotBoolean: rejected('AbortAttemptResponse', {ok: 1}),
  booleanIsNotNumber: rejected('TurnProposedPlan', {
    blockId: 'proposed-plan',
    planId: `plan_${'a'.repeat(24)}`,
    revision: true,
    format: 'markdown',
    text: 'Execute the verified plan.',
  }),
  validProjectionRefs: assertConversationSyncSchema(
    'SnapshotProjectionReferences', {'turn-a': {
      content: 'text:terminal',
      roundThinking: {'call-a': 'thinking:round-a'},
    }},
  )['turn-a'].content === 'text:terminal',
  emptyProjectionRefs: rejected('SnapshotProjectionReferences', {}),
  emptyContentBlock: rejected(
    'SnapshotProjectionReferences', {'turn-a': {content: ''}},
  ),
  emptyThinkingRefs: rejected(
    'SnapshotProjectionReferences', {'turn-a': {roundThinking: {}}},
  ),
  oversizedProjectionRefs: rejected(
    'SnapshotProjectionReferences', oversizedProjectionRefs,
  ),
  oversizedThinkingRefs: rejected(
    'SnapshotProjectionReferences', {'turn-a': {
      roundThinking: oversizedThinkingRefs,
    }},
  ),
}));
"""
    completed = subprocess.run(
        ["node", "-e", harness, bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "valid": True,
        "empty": True,
        "badDigest": True,
        "extraField": True,
        "badDictionaryKey": True,
        "oversized": True,
        "numberIsNotBoolean": True,
        "booleanIsNotNumber": True,
        "validProjectionRefs": True,
        "emptyProjectionRefs": True,
        "emptyContentBlock": True,
        "emptyThinkingRefs": True,
        "oversizedProjectionRefs": True,
        "oversizedThinkingRefs": True,
    }


def test_generated_browser_fast_path_matches_python_diagnostics():
    """All generated schemas agree across runtimes on representative JSON."""
    if not shutil.which("node"):
        pytest.skip("node is required")
    bundle = native_module_path(
        "conversation-sync-contract-validation.js",
        ROOT / "frontend/src/api/conversation-sync.generated.ts",
    )
    digest = "sha256:" + "a" * 64
    minimal_turn = {
        "turnId": "turn-a",
        "conversationId": "conv-a",
        "laneId": "main",
        "ordinal": 1,
        "actor": "assistant",
        "kind": "reply",
        "runId": "run-a",
        "status": "completed",
        "projection": {
            "content": "done",
            "segments": [
                {"type": "text", "blockId": "text:terminal", "text": "done"},
                {
                    "type": "tool_use",
                    "blockId": "tool:call-a",
                    "id": "call-a",
                    "name": "search",
                    "input": {"query": "evidence"},
                    "result": {"content": "found", "status": "done"},
                },
            ],
        },
        "projectionRevision": 1,
        "settlement": {},
        "createdAt": 1,
        "updatedAt": 1,
    }
    minimal_snapshot = {
        "ok": True,
        "contract": "tofu.conversation-sync.snapshot/v1",
        "conversationId": "conv-a",
        "conversationRevision": 0,
        "syncSeq": 0,
        "cursor": "cursor-a",
        "serverBootId": "boot-a",
        "heartbeatIntervalMs": 1_000,
        "settings": {},
        "turns": [],
        "attempts": [],
        "queueItems": [],
        "pushWithheld": False,
    }
    candidates = (
        None,
        False,
        True,
        -1,
        0,
        1,
        1.5,
        "",
        "value",
        digest,
        [],
        [None],
        [0, "value"],
        {},
        {"undeclared": None},
        {"toolContent": digest},
        {"toolContent": digest, "results": digest},
        {"toolContent": "invalid"},
        {"type": "text", "blockId": "text:1", "text": "hello"},
        minimal_turn,
        minimal_snapshot,
        {**minimal_snapshot, "turns": [minimal_turn]},
        {**minimal_snapshot, "heartbeatIntervalMs": 999},
        {**minimal_snapshot, "unexpected": True},
    )
    cases = [
        [
            schema_name,
            candidate,
            not contract_validation._validate(schema, candidate, "$"),
        ]
        for schema_name, schema in OPENAPI_SCHEMAS.items()
        for candidate in candidates
    ]
    harness = r"""
global.window = globalThis;
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const mismatches = [];
for (const [schema, value, expected] of cases) {
  let actual = true;
  try {
    assertConversationSyncSchema(schema, value);
  } catch (error) {
    if (!(error instanceof ConversationSyncContractError)) throw error;
    actual = false;
  }
  if (actual !== expected) mismatches.push({schema, value, expected, actual});
}
console.log(JSON.stringify({caseCount: cases.length, mismatches}));
"""
    completed = subprocess.run(
        ["node", "-e", harness, bundle],
        cwd=ROOT,
        input=json.dumps(cases, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {"caseCount": len(cases), "mismatches": []}


def test_projection_patch_copies_a_shared_terminal_round_before_updating_it():
    if not shutil.which("node"):
        pytest.skip("node is required")
    reference_bundle = native_module_path(
        "snapshot-tool-document-refs-for-patch.js",
        SOURCE,
    )
    patch_bundle = native_module_path(
        "projection-patch-for-shared-documents.js",
        ROOT / "frontend/src/core/projection-patch.ts",
    )
    harness = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));
const resultsKey = `sha256:${'b'.repeat(64)}`;
const sharedResults = [{id: 1}];
const makeTurn = (ordinal) => ({
  status: 'interrupted',
  projection: {
    toolRounds: [{
      toolCallId: `call-${ordinal}`,
      _snapshotDocumentRefs: {results: resultsKey},
    }],
    segments: [],
  },
});
const materialized = materializeSnapshotReferences({
  turns: [makeTurn(1), makeTurn(2)],
  sharedToolDocuments: {[resultsKey]: sharedResults},
});
const first = materialized.turns[0].projection;
const second = materialized.turns[1].projection;
const patched = applyProjectionPatch(first, {
  version: 1,
  operations: [{
    op: 'append',
    path: ['toolRounds', 0, 'results'],
    value: [{id: 2}],
  }],
});
console.log(JSON.stringify({
  startedShared: first.toolRounds[0].results === second.toolRounds[0].results,
  copiedProjection: patched !== first,
  copiedRounds: patched.toolRounds !== first.toolRounds,
  copiedRound: patched.toolRounds[0] !== first.toolRounds[0],
  copiedResults: patched.toolRounds[0].results !== sharedResults,
  patchedLength: patched.toolRounds[0].results.length,
  firstLength: first.toolRounds[0].results.length,
  secondLength: second.toolRounds[0].results.length,
}));
"""
    completed = subprocess.run(
        ["node", "-e", harness, reference_bundle, patch_bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "startedShared": True,
        "copiedProjection": True,
        "copiedRounds": True,
        "copiedRound": True,
        "copiedResults": True,
        "patchedLength": 2,
        "firstLength": 1,
        "secondLength": 1,
    }
