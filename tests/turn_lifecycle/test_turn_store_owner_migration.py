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
    native_module_graph,
    runtime_section,
    runtime_section_names,
)
from tests._jsdom import require_node_modules
from tests.frontend.test_frontend_conversation_surface_vite import (
    conversation_bundle as conversation_bundle,
    test_surface_reuses_turn_and_block_nodes_across_revisions as exercise_keyed_nodes,
    test_surface_hands_optimistic_turn_to_durable_identity_in_place as exercise_presentation_handoff,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
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


def test_typed_runtime_reuses_owners_and_exposes_command_capabilities():
    require_node_modules('vite')
    bundle = native_module_graph([
        ('.native/runtime-capabilities.js', 'frontend/src/core/turn-runtime.ts'),
        ('.native/runtime-state-owner.js', 'frontend/src/core/turn-state.ts'),
        ('.native/runtime-render-owner.js', 'frontend/src/core/turn-render.ts'),
        ('.native/runtime-presentation-owner.js', 'frontend/src/core/turn-presentation.ts'),
    ])
    script = r"""
global.window = globalThis;
require(process.argv[1]);
const runtime = createConversationTurnRuntime({ api: {} });
const owners = {
  emptyState: createTurnState, reducer: reduceTurnState, createStore: createTurnStore,
  renderInto: renderTurnStateInto, finishPresentation: presentTurnFinish,
  resumeOptions: resumeTurnOptions,
};
const commands = ['submit', 'runOperation', 'hydrateConversation', 'wakeConversation',
  'submitConversation', 'submitBranch', 'operateConversation', 'executeConversationPlan',
  'appendSettledConversationTurn', 'updateConversationTurn', 'mutateConversationFileChanges',
  'createBranchLane', 'deleteBranchLane', 'deleteConversationTurns', 'cancelQueuedTurn',
  'markCommandPending', 'markCommandFailed', 'abortConversation', 'abortAttempt'];
console.log(JSON.stringify({
  sharedOwners: Object.entries(owners).every(([name, owner]) => runtime[name] === owner),
  callableCommands: commands.every(name => typeof runtime[name] === 'function'),
  frozen: Object.isFrozen(runtime),
}));
"""
    result = subprocess.run(
        [shutil.which('node'), '-e', script, bundle], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        'sharedOwners': True, 'callableCommands': True, 'frozen': True,
    }


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


def test_renderer_identity_is_projection_id_not_message_index(conversation_bundle):
    """Verify the shipped graph, independent of the renderer's module layout."""
    exercise_keyed_nodes(conversation_bundle)
    exercise_presentation_handoff(conversation_bundle)


def test_retained_scroll_adapter_has_no_parallel_surface_latches():
    adapter = runtime_section('main/conversation_turn_store.js')
    scroll_owner = runtime_section('core.js')
    assert "return document.getElementById('chatContainer');" in adapter
    assert 'captureScroll() {' not in adapter
    assert 'restoreScroll(' not in adapter
    # User intent and history-window behavior are exercised by the typed owner
    # and shipped-browser tests. Retained compatibility must not regain scroll
    # authority, including the old mobile keyboard path.
    mobile = runtime_section('main/main_folders_mobile.js')
    for retired in ('_followSuspended', '_scrollRafId', '_armFollowSuspensionListeners',
                    'cc.scrollTop =', 'c.scrollTop ='):
        assert retired not in scroll_owner
        assert retired not in mobile
    for retired in (
        '_explicitBottomLatch', '_openScrollConvId', '_initialSwitchLoad',
    ):
        assert retired not in adapter
        assert retired not in scroll_owner


def test_main_bridge_does_not_export_turn_internals():
    require_node_modules('typescript')
    script = r"""
const ts = require('typescript');
const config = ts.readConfigFile('tsconfig.json', ts.sys.readFile);
if (config.error) throw new Error(ts.flattenDiagnosticMessageText(config.error.messageText, '\n'));
const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, process.cwd());
const program = ts.createProgram(parsed.fileNames, parsed.options);
const checker = program.getTypeChecker();
const windowSymbol = checker.resolveName('Window', undefined, ts.SymbolFlags.Type, false);
if (!windowSymbol) throw new Error('The browser Window contract is missing');
const bridge = checker.getPropertyOfType(checker.getDeclaredTypeOfSymbol(windowSymbol), 'TofuModules');
if (!bridge) throw new Error('The public Window.TofuModules contract is missing');
const type = checker.getNonNullableType(checker.getTypeOfSymbolAtLocation(bridge, bridge.valueDeclaration));
console.log(JSON.stringify(checker.getPropertiesOfType(type).map(property => property.name)));
"""
    result = subprocess.run(
        [shutil.which('node'), '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    bridge = set(json.loads(result.stdout))
    assert {'version', 'collectDiagnostics', 'invokeFeature'} <= bridge
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
    node = shutil.which('node')
    require_node_modules('vite')
    bundle = native_module_path(
        '.native/turn-actions.js',
        ROOT / 'frontend/src/conversation/presentation/view-model/turn-metadata.ts',
    )
    script = r"""
(0, eval)(require('fs').readFileSync(process.argv[1], 'utf8'));
const resume = (finish, status='failed', pending=null) => actionsFor(
  'assistant', 'main', pending, finish, {}, status, undefined, false, false,
).filter(action => action.action === 'resume');
console.log(JSON.stringify({
  missing: resume(null),
  inferred: resume({finishReason:'length', canResume:true}),
  granted: resume({resumeOptions:[{operation:'resume'}]}),
  busy: resume({resumeOptions:[{operation:'resume'}]}, 'running'),
  pending: resume({resumeOptions:[{operation:'resume'}]}, 'failed', 'retry'),
}));
"""
    result = subprocess.run([node, '-e', script, bundle], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        'missing': [], 'inferred': [], 'busy': [],
        'granted': [{'action': 'resume', 'operation': 'resume', 'disabled': False}],
        'pending': [{'action': 'resume', 'operation': 'resume', 'disabled': True}],
    }
