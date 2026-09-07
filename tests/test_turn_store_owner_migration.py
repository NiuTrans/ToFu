"""Turn ownership and retained-runtime migration ratchets.

Behavioral coverage for the reducer, transport, projection, recovery,
presentation and renderer lives in ``test_frontend_attempt_stream_vite.py``
and bundles the TypeScript modules that production executes.  This file pins
the retained adapter boundary: runtime code may inject ambient
UI services, but it must never regain its own Turn implementation.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import (
    native_module_path,
    runtime_section,
    runtime_section_names,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
ROLE_AVATAR_BUNDLE = native_module_path(
    '.native/role-avatar-icons.js',
    ROOT / 'frontend/src/core/role-avatar-icons.ts',
)


def test_role_avatar_owner_builds_base_aware_immutable_assets():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    script = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const icons = createRoleAvatarIcons('/tofu');
console.log(JSON.stringify({ icons, frozen: Object.isFrozen(icons) }));
"""
    result = subprocess.run(
        [node, '-e', script, ROLE_AVATAR_BUNDLE],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['frozen'] is True
    expected = {
        'plannerHtml': ('tofu-planner.svg', 'alt="Planner"'),
        'criticHtml': ('tofu-critic.svg', 'alt="Critic"'),
        'workerHtml': ('tofu-worker.svg', 'alt="Worker"'),
        'userHtml': ('onigiri.svg', 'alt="You"'),
    }
    for key, (filename, alt) in expected.items():
        markup = payload['icons'][key]
        assert f'/tofu/static/icons/{filename}?v=20260402b' in markup
        assert alt in markup


def test_retained_turn_adapter_uses_only_the_typed_role_asset_port():
    adapter = runtime_section('main/conversation_turn_store.js')
    assert 'createRoleAvatarIcons(' in adapter
    for retired in (
        '_TOFU_PLANNER_SVG', '_TOFU_CRITIC_SVG',
        '_TOFU_WORKER_SVG', '_USER_AVATAR_SVG',
    ):
        assert retired not in adapter


def test_typed_runtime_composes_every_turn_domain_owner():
    source = (ROOT / 'frontend/src/core/turn-runtime.ts').read_text(
        encoding='utf-8')

    for owner in (
        "from './conversation-sync'",
        "from './turn-command'",
        "from './turn-projection'",
        "from './turn-presentation'",
        "from './turn-render'",
        "from './turn-state'",
    ):
        assert owner in source
    for public_operation in (
        'emptyState: createTurnState',
        'reducer: reduceTurnState',
        'createStore: createTurnStore',
        'renderInto: renderTurnStateInto',
        'finishPresentation: presentTurnFinish',
        'resumeOptions: resumeTurnOptions',
        'hydrateConversation',
        'submitConversation',
        'operateConversation',
    ):
        assert public_operation in source


def test_retained_turn_adapter_has_no_shadow_state_or_transport():
    source = runtime_section('main/conversation_turn_store.js')

    assert 'runtimeScope.ConversationTurnStore = createConversationTurnRuntime({' in source
    for forbidden in (
        'function emptyState(',
        'function reducer(',
        'function createStore(',
        'function connect(',
        'function renderInto(',
        'function finishPresentation(',
        'function commandId(',
        'new EventSource(',
        'global.TofuModules',
        'runtimeScope.ConversationTurnStore = Object.freeze',
    ):
        assert forbidden not in source


def test_loaded_turn_store_intercepts_every_retained_conversation_repaint():
    """Retired whole-conversation render owners cannot re-enter the graph."""
    adapter = runtime_section('main/conversation_turn_store.js')
    names = set(runtime_section_names())
    assert 'conv_view.js' not in names
    assert 'ui/chat_render.js' not in names
    assert 'runtimeScope.requestAuthoritativeConversationRender = function' in adapter
    assert '_conversationSurfaceController.render(' in adapter
    assert 'ConvView' not in adapter
    assert 'renderChat(' not in adapter


def test_renderer_identity_is_projection_id_not_message_index():
    renderer = (ROOT / 'frontend/src/conversation/ui/conversation-surface.ts').read_text(
        encoding='utf-8')
    assert "directKeyedChildren(turnsContainer, 'presentationId')" in renderer
    assert 'turnNode.dataset.presentationId = presentationId' in renderer
    assert 'turnNode.dataset.turnId = turn.turnId' in renderer
    assert "directKeyedChildren(containerNode, 'blockId')" in renderer
    assert 'node.dataset.blockId = block.blockId' in renderer
    assert "getElementById('msg-'" not in renderer
    assert 'activeTaskId' not in renderer
    assert 'streaming-msg' not in renderer
    assert 'data-msg-id' not in renderer


def test_surface_host_owns_scroll_follow_without_legacy_renderer_latches():
    adapter = runtime_section('main/conversation_turn_store.js')
    renderer = (
        ROOT / 'frontend/src/conversation/ui/conversation-surface.ts'
    ).read_text(encoding='utf-8')
    scroll_owner = runtime_section('core.js')
    assert "return document.getElementById('chatContainer');" in adapter
    assert 'captureScroll() {' not in adapter
    assert 'restoreScroll(' not in adapter
    assert 'createConversationViewportPort(' in renderer
    assert 'let following = true;' in renderer
    assert "viewport.addEventListener('wheel', onWheel" in renderer
    assert 'const anchor = scrollAnchor?.capture(root);' in renderer
    assert 'scrollAnchor?.restore(root, anchor);' in renderer
    assert 'ownedScrollAnchor?.dispose?.();' in renderer
    # Controller/viewport arbitration is exercised against the compiled owner
    # in test_frontend_conversation_surface_vite; keep this retained-boundary
    # ratchet focused on the adapter and legacy fallback it actually owns.

    jump_start = scroll_owner.index('function scrollChatToBottom() {')
    jump_end = scroll_owner.index('function _updateScrollToBottomBtn()', jump_start)
    jump = scroll_owner[jump_start:jump_end]
    assert '_followSuspended = false;' in jump
    assert 'c.scrollTop = c.scrollHeight;' in jump
    assert '_withInstantScroll' in jump
    for retired in (
        '_explicitBottomLatch', '_openScrollConvId', '_initialSwitchLoad',
    ):
        assert retired not in adapter
        assert retired not in scroll_owner


def test_main_bridge_does_not_export_turn_internals():
    source = (ROOT / 'frontend/src/main.ts').read_text(encoding='utf-8')
    bridge = source[source.index('export interface TofuModuleBridge'):
                    source.index('connectFeatureRuntime(')]
    for removed in (
        'createConversationTurnRuntime',
        'createAttemptEventStream',
        'createTurnState',
        'reduceTurnState',
        'applyTurnStateProjection',
        'presentTurnFinish',
        'buildTurnSubmitRequest',
        'buildTurnOperationRequest',
    ):
        assert removed not in bridge


def test_turn_continue_affordance_reads_only_server_resume_options():
    source = (ROOT / 'frontend/src/conversation/presentation/conversation-view-model.ts').read_text()
    start = source.index('function actionsFor(')
    end = source.index('interface LaneOwner', start)
    gate = source[start:end]

    assert 'finish?.resumeOptions?.[0]' in gate
    assert "actions.push({ action: 'resume'" in gate
    for retired_inference in (
        'computeTurnSettlement', 'continueButtonForSettlement',
        'finishReason', '_trimmedToolRoundCount',
    ):
        assert retired_inference not in gate
