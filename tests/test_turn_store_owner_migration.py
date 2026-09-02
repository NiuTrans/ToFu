"""Turn ownership and retained-runtime migration ratchets.

Behavioral coverage for the reducer, transport, projection, recovery,
presentation and renderer lives in ``test_frontend_attempt_stream_vite.py``
and bundles the TypeScript modules that production executes.  This file pins
the retained adapter boundary: runtime code may inject ambient
UI services, but it must never regain its own Turn implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section, runtime_section_names


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


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


def test_renderer_identity_is_turn_id_not_message_index():
    renderer = (ROOT / 'frontend/src/conversation/ui/conversation-surface.ts').read_text(
        encoding='utf-8')
    assert "directKeyedChildren(turnsContainer, 'turnId')" in renderer
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


def test_autopilot_vu_stream_is_a_transient_turn_not_a_message_writer():
    source = runtime_section('ui/streaming_render.js')

    assert 'createAutopilotVuTransientTurn({' in source
    assert 'reduceAutopilotVuTransientTurn(' in source
    assert 'settleAutopilotVuTransientTurn(' in source
    assert 'ConversationTransientTurns?.remove?.' in source
    assert 'ConversationTurnStore' in source
    assert 'hydrateConversation(conv)' in source
    for forbidden in (
        'conv.messages.push(',
        'conv.messages.splice(',
        'conv.messages.pop(',
        'insertAdjacentHTML(',
        'getElementById("streaming-msg")',
        'ConvCache.put(conv)',
        'saveConversations(convId)',
    ):
        assert forbidden not in source[:source.index(
            'function _applyAutopilotRunConcluded')]
