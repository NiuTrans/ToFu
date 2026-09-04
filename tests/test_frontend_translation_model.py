"""Turn-native translation projection and retired compatibility contracts."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
VIEW_MODEL = (
    ROOT / 'frontend/src/conversation/presentation/conversation-view-model.ts'
)
VIEW_MODEL_BUNDLE = native_module_path(
    '.native/turn-native-translation-view-model.js', VIEW_MODEL,
)


def _run_node(script: str) -> str:
    process = subprocess.run(
        ['node', '-e', script], cwd=ROOT, capture_output=True, text=True,
        timeout=60,
    )
    output = (process.stdout or '') + (process.stderr or '')
    assert process.returncode == 0, output
    return output


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_turn_projection_is_the_translation_display_authority():
    script = r'''
const fs = require('fs');
eval(fs.readFileSync(OWNER_PATH, 'utf8'));
const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);
const segment = {
  type: 'text', blockId: 'text:terminal', text: 'Original answer',
  translatedText: '翻译答案', deliverable: true, terminal: true,
};
const turn = {
  turnId: 'turn-a', conversationId: 'conv-a', laneId: 'main', ordinal: 1,
  actor: 'assistant', kind: 'reply', status: 'completed', currentAttemptId: null,
  projection: {
    segments: [segment], content: 'Original answer',
    translatedContent: '翻译答案', _translateDone: true,
  },
  projectionRevision: 2, settlement: { outcome: 'completed' },
  createdAt: 1, updatedAt: 2,
};
const state = {
  conversationId: 'conv-a', conversationRevision: 2, transport: 'live',
  turnsById: { 'turn-a': turn }, laneOrder: { main: ['turn-a'] },
  attemptsById: {}, queueItems: [], pendingEventsByTurn: {}, commandPending: {},
  liveRoundUsageByTurn: {},
};
const before = JSON.stringify(state);
const translated = selectConversationViewModel(state, {}, {
  translationModeByTurn: new Map([['turn-a', 'translated']]),
}).mainLane.turns[0].blocks[0];
const original = selectConversationViewModel(state, {}, {
  translationModeByTurn: new Map([['turn-a', 'original']]),
}).mainLane.turns[0].blocks[0];

check('typed_view_model_selects_the_turn', translated.kind === 'text');
check('authoritative_translation_is_the_alternative',
  translated.translatedMarkdown === '翻译答案');
check('translated_mode_selects_translated_projection',
  translated.displayMarkdown === '翻译答案');
check('original_mode_selects_authoritative_original',
  original.displayMarkdown === 'Original answer');
check('display_choice_does_not_mutate_turn_facts', JSON.stringify(state) === before);
check('ui_mode_is_not_persisted_on_projection',
  !Object.hasOwn(turn.projection, '_showingTranslation'));

console.log(checks.join('\n'));
if (checks.some(line => line.startsWith('FAIL'))) process.exitCode = 1;
'''.replace('OWNER_PATH', json.dumps(VIEW_MODEL_BUNDLE))
    output = _run_node(script)
    assert output.count('PASS') == 6, output
