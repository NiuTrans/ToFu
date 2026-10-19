"""Retained shell action ports have no Studio selection or workspace prerequisite."""
import subprocess
from pathlib import Path

import pytest
from tests._runtime_sections import shipped_source_text
from tests.frontend.test_frontend_workspace_attachment_results import _function

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_project_action_opens_picker_before_optional_bookkeeping():
    source = shipped_source_text('frontend/src/runtime/sections/main/main_toolbar_ui.js')
    action = _function(source, 'function setChatMode(mode) {')
    toggle = _function(source, 'function toggleChatModeMenu(e) {')
    script = r'''
const assert=require('assert'),order=[];
const runtimeScope={openProjectModal:()=>order.push('open')};
const _applyChatModeUI=mode=>{assert.equal(mode,'chat');order.push('chat')};
const captureActiveConversationSettings=()=>order.push('save');
__ACTION__
__TOGGLE__
setChatMode('studio'); assert.deepStrictEqual(order,['open','chat','save']);
order.length=0; toggleChatModeMenu({stopPropagation(){order.push('stop')}});
assert.deepStrictEqual(order,['stop','open']);
'''.replace('__ACTION__', action).replace('__TOGGLE__', toggle)
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_attachment_hooks_do_not_change_flow_model_or_persist_another_selection():
    source = shipped_source_text('frontend/src/runtime/sections/main/main_toolbar_ui.js')
    attached = _function(source, 'function onProjectAttached() {')
    cleared = _function(source, 'function onProjectCleared() {')
    script = r'''
const assert=require('assert');let painted=0;
const document={},t=key=>key;
const paintOptionalProjectAttachment=()=>painted++;
const captureActiveConversationSettings=()=>{throw Error('unexpected persistence')};
const _applyChatModeUI=()=>{throw Error('unexpected capability mutation')};
__ATTACHED__
__CLEARED__
onProjectAttached();onProjectCleared();assert.equal(painted,2);
'''.replace('__ATTACHED__', attached).replace('__CLEARED__', cleared)
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
