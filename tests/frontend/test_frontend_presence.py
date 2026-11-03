"""Project-root adapter contract for the typed conversation-first surface.

The retired CollabBar markup is not the contract. The current presence adapter
must still resolve an armed New Chat project through the same projectState
fallback used by the composer, then pass that root to the typed surface.
"""
from __future__ import annotations

import subprocess

import pytest

from tests._runtime_sections import ROOT, runtime_section_path

pytestmark = pytest.mark.unit


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const root = process.argv[2];
const {JSDOM} = require(path.join(root, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!doctype html><body><div id="presenceStrip"></div><div id="convList"></div></body>', {url: 'http://localhost'});
global.window = dom.window;
global.document = dom.window.document;
global.runtimeScope = {};
global.conversations = [];
global.activeConvId = null;
global.projectState = {active: true, path: '/proj/B', extraRoots: []};
global.getActiveConv = () => conversations.find(row => row.id === activeConvId) || null;
global._getConvProjectPath = row => row?.projectPath || '';
global.getActiveFolderId = () => null;
global.Api = {project: {}};
global.t = key => key;
global._projectConversationNavigation = {checkpoint: () => () => true};
const scopes = [];
global.createProjectCoordinationSurface = ports => {
  scopes.push(ports);
  return {refresh() {}, destroy() {}, coordinatorId() { return ''; }};
};
global.createSameTaskReadJoin = () => (_key, _signal, read) => read({});
const emptyEntry = () => ({
  refresh() {}, destroy() {}, home() { return ''; }, location() { return ''; },
  conversationId() { return ''; }, ensure() {},
});
global.createGlobalRouterEntry = emptyEntry;
global.createRouterWatchdogEntry = emptyEntry;
global.createCoordinatorConversationGroups = () => ({refresh() {}, destroy() {}});
eval(fs.readFileSync(process.argv[1], 'utf8'));
const out = [];
function check(name, condition) { out.push((condition ? 'PASS ' : 'FAIL ') + name); }
conversations = [{id: 'saved', projectPath: '/proj/A'}];
activeConvId = 'saved';
check('active_conversation_project', scopes[0].scope().path === '/proj/A');
activeConvId = null;
check('armed_new_chat_project', scopes[0].scope()?.path === '/proj/B');
check('new_chat_has_no_conversation_identity', scopes[0].scope()?.currentConversationId === '');
projectState = {active: false, path: '', extraRoots: []};
check('cleared_project_has_no_surface_scope', scopes[0].scope() === null);
console.log(out.join('\n'));
dom.window.close();
"""


def _run(tmp_path, presence_src: str | None = None) -> str:
    proc = subprocess.run(
        ['node', '-e', _HARNESS,
         presence_src or runtime_section_path('presence.js'), str(ROOT)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


def test_project_surface_uses_armed_new_chat_project(tmp_path):
    output = _run(tmp_path)
    assert 'FAIL' not in output, output
    assert output.count('PASS') == 4, output


def test_projectstate_fallback_is_load_bearing(tmp_path):
    presence_js = runtime_section_path('presence.js')
    src = (tmp_path / 'presence.js')
    original = open(presence_js, encoding='utf-8').read()
    needle = "if (!path && typeof projectState !== 'undefined' && projectState?.active) {"
    assert original.count(needle) == 1, 'projectState fallback drifted'
    src.write_text(original.replace(needle, 'if (!path && false) {', 1),
                   encoding='utf-8')
    output = _run(tmp_path, str(src))
    assert 'FAIL armed_new_chat_project' in output, output
    assert 'PASS active_conversation_project' in output, output
    assert 'PASS cleared_project_has_no_surface_scope' in output, output
    assert open(presence_js, encoding='utf-8').read() == original
