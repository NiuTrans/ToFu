"""Turn navigation is keyed by durable Turn IDs and never by message position."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from tests._runtime_sections import runtime_section, runtime_section_path

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _deps_available() -> bool:
    return bool(shutil.which('node')) and os.path.isdir(
        os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const { JSDOM } = require(path.join(process.argv[2], 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!doctype html><body>
  <div id="chatContainer"><div id="chatInner"></div></div>
  <nav id="turnNav"></nav>
</body>`);
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.runtimeScope = dom.window;
globalThis.console = console;

const output = [];
function check(name, condition) {
  output.push((condition ? 'PASS ' : 'FAIL ') + name);
}

const turns = (prefix) => ({
  [`${prefix}-h1`]: {
    turnId: `${prefix}-h1`, actor: 'human', laneId: 'main',
    projectionRevision: 1, projection: { content: 'first prompt' },
  },
  [`${prefix}-a1`]: {
    turnId: `${prefix}-a1`, actor: 'assistant', laneId: 'main',
    projectionRevision: 2,
    projection: { modifiedFiles: 1, modifiedFileList: [{ path: 'src/app.ts' }] },
  },
  [`${prefix}-h2`]: {
    turnId: `${prefix}-h2`, actor: 'human', laneId: 'main',
    projectionRevision: 3,
    projection: { segments: [{ type: 'text', text: 'second prompt' }] },
  },
  [`${prefix}-a2`]: {
    turnId: `${prefix}-a2`, actor: 'assistant', laneId: 'main',
    projectionRevision: 4, projection: {},
  },
});
function state(prefix, revision) {
  return {
    conversationRevision: revision,
    turnsById: turns(prefix),
    laneOrder: { main: [
      `${prefix}-h1`, `${prefix}-a1`, `${prefix}-h2`, `${prefix}-a2`,
    ] },
  };
}
const states = new Map([['conv-a', state('a', 1)], ['conv-b', state('b', 1)]]);
let activeConv = { id: 'conv-a' };
let renderRequests = 0;
let scrolled = '';
globalThis.ConversationTurnStore = window.ConversationTurnStore = {
  ensureRuntimeStore(id) { return { getState() { return states.get(id); } }; },
};
globalThis.getActiveConv = () => activeConv;
globalThis.prefersReducedMotion = () => true;
globalThis._getChatContainer = () => document.getElementById('chatContainer');
globalThis.requestAnimationFrame = (callback) => { callback(); return 1; };
globalThis.requestAuthoritativeConversationRender = window.requestAuthoritativeConversationRender = () => {
  renderRequests += 1;
  const missing = document.createElement('article');
  missing.dataset.turnId = 'b-h2';
  missing.scrollIntoView = () => { scrolled = 'b-h2'; };
  document.getElementById('chatInner').appendChild(missing);
  return true;
};

const inner = document.getElementById('chatInner');
function seedNodes(prefix) {
  inner.replaceChildren();
  for (const suffix of ['h1', 'a1', 'h2', 'a2']) {
    const node = document.createElement('article');
    node.dataset.turnId = `${prefix}-${suffix}`;
    node.dataset.top = suffix === 'h1' ? '-100' : suffix === 'h2' ? '20' : '80';
    node.getBoundingClientRect = () => ({ top: Number(node.dataset.top), height: 20 });
    node.scrollIntoView = () => { scrolled = node.dataset.turnId; };
    inner.appendChild(node);
  }
}
document.getElementById('chatContainer').getBoundingClientRect = () => ({ top: 0, height: 200 });

(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

seedNodes('a');
buildTurnNav(activeConv);
let dots = [...document.querySelectorAll('.turn-dot')];
check('two_human_turns', dots.length === 2);
check('stable_ids_rendered',
  dots.map((dot) => dot.dataset.turnId).join(',') === 'a-h1,a-h2');
check('no_positional_attribute', dots.every((dot) => !dot.hasAttribute('data-msg-idx')));
check('write_metadata_derived_from_reply',
  dots[0].classList.contains('turn-dot-writes') && dots[0].title.includes('app.ts'));
check('segment_preview_used', dots[1].title.includes('second prompt'));

dots[1].click();
check('click_scrolls_stable_turn', scrolled === 'a-h2');
updateActiveTurn();
check('active_dot_uses_turn_geometry',
  document.querySelector('.turn-dot.active')?.dataset.turnId === 'a-h2');

const firstDot = dots[0];
buildTurnNav(activeConv);
check('unchanged_revision_reuses_nav', document.querySelector('.turn-dot') === firstDot);

activeConv = { id: 'conv-b' };
inner.replaceChildren();
buildTurnNav(activeConv);
dots = [...document.querySelectorAll('.turn-dot')];
check('conversation_switch_invalidates_fingerprint',
  dots.map((dot) => dot.dataset.turnId).join(',') === 'b-h1,b-h2');
dots[1].click();
check('missing_node_requests_authoritative_render', renderRequests === 1);
check('post_render_scroll_uses_same_turn_id', scrolled === 'b-h2');

console.log(output.join('\n'));
"""


def _run(section_path: str) -> str:
    proc = subprocess.run(
        ['node', '-e', _HARNESS, section_path, ROOT],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'{proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _deps_available(), reason='node + jsdom are required')
def test_turn_navigation_uses_stable_turn_identity():
    output = _run(runtime_section_path('ui/turn_nav.js'))
    failures = [line for line in output.splitlines() if line.startswith('FAIL')]
    assert not failures, output
    assert output.count('PASS') >= 11, output


@pytest.mark.skipif(not _deps_available(), reason='node + jsdom are required')
def test_NEUTER_turn_id_binding_is_load_bearing():
    source = runtime_section('ui/turn_nav.js')
    needle = 'button.dataset.turnId = turn.turnId;'
    assert source.count(needle) == 1
    with tempfile.TemporaryDirectory(prefix='tofu-turn-nav-neuter-') as temp:
        path = os.path.join(temp, 'turn_nav.js')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(source.replace(
                needle, "button.dataset.turnId = String(index);", 1))
        output = _run(path)
    assert 'FAIL stable_ids_rendered' in output, output


def test_turn_navigation_source_has_no_positional_renderer_dependency():
    source = runtime_section('ui/turn_nav.js')
    for retired in (
        'conv.messages', 'data-msg-idx', 'getElementById("msg-',
        "getElementById('msg-", 'renderMessage(', 'activeStreams',
        'streaming-msg', '_lazyRenderedFrom', '_lazyRenderedTo',
    ):
        assert retired not in source
