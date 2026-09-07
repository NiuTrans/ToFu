"""Agent-mode selector: one UI state over the current wire flags."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from tests._jsdom import JS_DIR, run_harness


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent
TOOLBAR = str(Path(JS_DIR) / "main" / "main_toolbar_ui.js")
AGENT_MODE = str(
    ROOT / "frontend" / "src" / "conversation" / "application" / "agent-mode.ts"
)
PLAN_DECISION_BAR = str(
    ROOT / "frontend" / "src" / "conversation" / "ui" / "plan-decision-bar.ts"
)
FLOW_PICKER = ROOT / "frontend" / "src" / "features" / "orchestration" / "flow-picker.ts"
VITE_TEST_BUNDLER = ROOT / "scripts" / "vite_test_bundle.mjs"


def test_desktop_and_mobile_group_plan_and_autopilot():
    from lib.application_shell_fragments import (
        inject_fragments,
        list_fragment_names,
        marker_for,
    )

    # Exercise the authored fragments through their closed-set production
    # assembler. The application-shell suite separately proves that the real
    # HTTP index owns exactly this marker set.
    html = inject_fragments(
        "\n".join(marker_for(name) for name in sorted(list_fragment_names()))
    )
    shell = BeautifulSoup(html, "html.parser")
    desktop = shell.select_one("#submenuAgentMode")
    assert desktop is not None
    assert desktop.select_one("#agentModeStandard") is not None
    assert desktop.select_one("#planModeToggle") is not None
    assert desktop.select_one("#autopilotToggle") is not None
    assert desktop.select_one("#endpointToggle") is None
    assert "toolbar.autonomousMode" not in html

    mobile = shell.select_one(".mobile-agent-mode-group")
    assert mobile is not None
    for element_id in (
        "mobileAgentModeStandard", "mobilePlanMode", "mobileAutopilot",
    ):
        assert mobile.select_one(f"#{element_id}") is not None
    assert mobile.select_one("#mobileEndpoint") is None


_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const fs = require('fs');
const ts = require('typescript');
const compiledAgentMode = ts.transpileModule(
  fs.readFileSync(process.argv[4], 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022,
                       module: ts.ModuleKind.CommonJS } },
).outputText;
const agentModeModule = { exports: {} };
new Function('module', 'exports', compiledAgentMode)(
  agentModeModule, agentModeModule.exports,
);
const {
  agentModeFlags: flags,
  normalizeAgentMode: normalize,
  normalizeConversationInteractionModes,
  resolveAgentMode: resolve,
} = agentModeModule.exports;
let busy = false;
let saves = 0;
let disarms = 0;
let flowCatalogState = 'loading';
let pendingFocusFrame = null;
const html = `<!doctype html><body>
  <div id="submenuAgentMode" class="toolbar-submenu open">
    <button id="agentModeToggle" class="submenu-trigger open" aria-expanded="true"></button>
    <span id="agentModeLabel"></span>
    <div id="agentModeMenu">
      <button id="agentModeStandard" class="agent-mode-item" data-mode="standard"
        role="radio" aria-checked="true"></button>
      <button id="planModeToggle" class="agent-mode-item" data-mode="plan"
        role="radio" aria-checked="false"></button>
      <button id="autopilotToggle" class="agent-mode-item" data-mode="autopilot"
        role="radio" aria-checked="false"></button>
    </div>
  </div>
  <span id="autopilotBadge" style="display:none"></span>
  <button class="mobile-agent-mode-item" data-mode="standard"></button>
  <button class="mobile-agent-mode-item" data-mode="plan"></button>
  <button id="flowToggle"></button>
  <div id="flowMenuStatus" hidden></div>
  <div id="flowMenuList"><button data-flow="flow-a"></button></div>
</body>`;
const conv = { id: 'conv-a' };
const { window, document, check, report } = setup({
  root: process.argv[3],
  html,
  targets: [process.argv[2]],
  globals: {
    runtimeScope: {},
    planMode: false,
    autopilotEnabled: false,
    activeFlow: 'flow-old',
    imageGenMode: true,
    humanGuidanceEnabled: false,
    agentModeFlags: flags,
    resolveAgentMode: resolve,
    normalizeAgentMode: normalize,
    getActiveConv: () => conv,
    convIsBusy: () => busy,
    _applyFlowUI: (value) => { global.activeFlow = value || ''; },
    _applyImageGenUI: (value) => { global.imageGenMode = !!value; },
    _applyHumanGuidanceUI: (value) => { global.humanGuidanceEnabled = !!value; },
    captureActiveConversationSettings: () => { saves += 1; },
    updateSubmenuCounts: () => {},
    debugLog: () => {},
    _orchestrationFlowCatalog: {
      status: () => ({ state: flowCatalogState }),
    },
    renderOrchestrationFlowCatalogNotice: (element, catalog) => {
      const visible = catalog.status().state !== 'ready';
      element.hidden = !visible;
      element.textContent = visible ? 'toolbar.flowCatalogLoading' : '';
      return visible;
    },
    projectOrchestrationFlowPickerItems: (custom) => custom.map((item) => ({
      flow: item.id, name: item.name, desc: 'toolbar.flowCustomDesc',
    })),
    orchestrationFlowPickerIcon: () => '',
    wireOrchestrationFlowPicker: () => true,
    Api: { chat: { disarmAutopilot: () => { disarms += 1; return Promise.resolve({}); } } },
  },
});
global.updateSubmenuCounts = window.updateSubmenuCounts = () => {};
const harnessRequestAnimationFrame = window.requestAnimationFrame;
const harnessGetComputedStyle = window.getComputedStyle.bind(window);
global.requestAnimationFrame = window.requestAnimationFrame = (callback) => {
  pendingFocusFrame = callback;
  return 1;
};

const conflictingPlan = normalizeConversationInteractionModes({
  planMode: true, autopilotEnabled: true,
  activeFlow: 'flow-old',
});
check('Conflict gives Plan fail-closed precedence',
  conflictingPlan.agentMode === 'plan' && conflictingPlan.activeFlow === '');
const conflictingFlow = normalizeConversationInteractionModes({
  autopilotEnabled: true, activeFlow: 'flow-old',
});
check('Conflict gives explicit Flow second precedence',
  conflictingFlow.agentMode === 'standard' && conflictingFlow.activeFlow === 'flow-old');
check('Unknown agent mode normalizes to Standard', normalize('unknown') === 'standard');

check('Plan selection accepted', setAgentMode('plan') === true);
check('Plan is the only wire mode', planMode && !autopilotEnabled);
check('Plan forces Human Guidance', humanGuidanceEnabled === true);
check('Plan clears Flow and image mode', activeFlow === '' && imageGenMode === false);
check('Plan trigger label projected', document.getElementById('agentModeLabel').textContent === 'toolbar.planMode');
check('Plan radio selected', document.querySelector('#agentModeMenu [data-mode="plan"]').getAttribute('aria-checked') === 'true');

setAgentMode('autopilot');
check('Autopilot is mutually exclusive', !planMode && autopilotEnabled);
setAgentMode('standard');
check('Standard clears both flags', !planMode && !autopilotEnabled);
check('Leaving Autopilot disarms durable marker', disarms === 1);

setAgentMode('plan');
busy = true;
check('In-flight turn rejects a mode change', setAgentMode('autopilot') === false);
check('Rejected switch preserves Plan', planMode && !autopilotEnabled);
check('In-flight turn rejects the separate Flow loop owner',
  setActiveFlow('flow-a') === false && activeFlow === '');
busy = false;
conv._genStartCtrl = {};
check('Start handshake also rejects an interaction-mode change',
  setAgentMode('autopilot') === false && planMode);
delete conv._genStartCtrl;

document.getElementById('submenuAgentMode').classList.remove('open');
document.getElementById('agentModeToggle').focus();
handleAgentModeMenuTriggerKey(new window.KeyboardEvent(
  'keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }));
check('Opening keyboard navigation defers focus until the menu is paintable',
  document.activeElement.id === 'agentModeToggle'
  && typeof pendingFocusFrame === 'function');
let visibilityChecks = 0;
window.getComputedStyle = (element) => {
  if (element.id === 'planModeToggle' && visibilityChecks++ === 0) {
    return { visibility: 'hidden' };
  }
  return harnessGetComputedStyle(element);
};
const firstFocusFrame = pendingFocusFrame;
if (typeof firstFocusFrame === 'function') firstFocusFrame();
check('Keyboard focus waits through the visibility transition',
  document.activeElement.id === 'agentModeToggle'
  && pendingFocusFrame !== firstFocusFrame);
if (typeof pendingFocusFrame === 'function') pendingFocusFrame();
check('Deferred keyboard focus lands on the selected Agent Mode',
  document.activeElement.id === 'planModeToggle');
global.requestAnimationFrame = window.requestAnimationFrame =
  harnessRequestAnimationFrame;
window.getComputedStyle = harnessGetComputedStyle;

_renderFlowMenu([]);
check('Flow loading notice excludes the contradictory empty state',
  !document.getElementById('flowMenuStatus').hidden
  && document.getElementById('flowMenuList').innerHTML === '');
flowCatalogState = 'ready';
_renderFlowMenu([]);
check('Flow empty state appears only after the catalogue settles empty',
  document.getElementById('flowMenuStatus').hidden
  && !!document.querySelector('#flowMenuList .agent-workflow-empty'));
_renderFlowMenu([{ id: 'flow-a', name: 'Flow A' }]);
check('Saved workflows replace the empty state',
  !!document.querySelector('#flowMenuList [data-flow="flow-a"]')
  && !document.querySelector('#flowMenuList .agent-workflow-empty'));

_setAgentModeLocked(true);
check('Desktop trigger locks in flight', document.getElementById('agentModeToggle').disabled);
check('Desktop menu closes when locked', !document.getElementById('submenuAgentMode').classList.contains('open'));
check('Mobile mode controls lock in flight', document.querySelector('.mobile-agent-mode-item').disabled);
check('Flow trigger locks with the other loop owners',
  document.getElementById('flowToggle').disabled);
check('Rendered Flow choices lock with the trigger',
  document.querySelector('#flowMenuList button').disabled);
check('Locked trigger advertises the locked tooltip hook',
  document.getElementById('agentModeToggle').getAttribute('data-i18n-title')
    === 'toolbar.agentModeLockedTooltip');
_setAgentModeLocked(false);
check('Unlock restores the base tooltip hook and the click target',
  document.getElementById('agentModeToggle').getAttribute('data-i18n-title')
    === 'toolbar.agentModeTooltip'
    && !document.getElementById('agentModeToggle').disabled);
check('Accepted transitions persist once each', saves === 4);
report();
"""


def test_agent_mode_transitions_are_atomic_and_lock_in_flight():
    run_harness(
        TOOLBAR,
        _HARNESS,
        extra_targets=[AGENT_MODE],
        expect_pass=30,
        label="agent mode selector",
    )


def test_catalog_notice_and_empty_state_are_mutually_exclusive_on_both_surfaces():
    toolbar = Path(TOOLBAR).read_text(encoding="utf-8")
    mobile = (Path(JS_DIR) / "mobile_panels.js").read_text(encoding="utf-8")
    for source in (toolbar, mobile):
        assert "catalogNoticeVisible" in source
        assert "catalogNoticeVisible ? ''" in source


def test_typed_flow_picker_preserves_projection_notice_and_keyboard_contracts(
    tmp_path: Path,
):
    """The migrated owner is exercised through the production Vite graph."""
    bundle = tmp_path / "flow-picker.cjs"
    compiled = subprocess.run(
        [
            str(VITE_TEST_BUNDLER), str(FLOW_PICKER), "--bundle",
            "--format=cjs", "--platform=node", f"--outfile={bundle}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr

    script = r"""
const { JSDOM } = require('jsdom');
const picker = require(process.argv[1]);
const translate = (key) => `t:${key}`;
const custom = [{ id: 'flow-a', name: 'Flow A' }];
const projected = picker.projectOrchestrationFlowPickerItems(custom, translate);
const saved = picker.projectOrchestrationSavedWorkflowItems(custom, translate);
const dom = new JSDOM(`<!doctype html><body>
  <div id="notice"></div>
  <div id="list">
    <button data-flow="a"></button>
    <button data-flow="b" aria-checked="true"></button>
    <button data-flow="c"></button>
  </div>
</body>`);
global.Element = dom.window.Element;
const document = dom.window.document;
const notice = document.getElementById('notice');
const list = document.getElementById('list');
const selected = [];
let escaped = 0;
const noticeShown = picker.renderOrchestrationFlowCatalogNotice(
  notice,
  { status: () => ({ state: 'failed', failure: new Error('offline') }) },
  translate,
);
picker.wireOrchestrationFlowPicker(list, {
  onSelect: (flow) => selected.push(flow),
  onEscape: () => { escaped += 1; },
});
const rows = [...list.querySelectorAll('[data-flow]')];
rows[1].focus();
rows[1].dispatchEvent(new dom.window.KeyboardEvent('keydown', {
  key: 'ArrowDown', bubbles: true, cancelable: true,
}));
rows[2].dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
rows[2].dispatchEvent(new dom.window.KeyboardEvent('keydown', {
  key: 'Escape', bubbles: true, cancelable: true,
}));
process.stdout.write(JSON.stringify({
  projected,
  projectedFrozen: Object.isFrozen(projected)
    && projected.every((item) => Object.isFrozen(item)),
  saved,
  savedFrozen: Object.isFrozen(saved) && Object.isFrozen(saved[0]),
  display: picker.orchestrationFlowPickerDisplayName('flow-a', custom, translate),
  staleDropped: picker.reconcileOrchestrationFlowSelection(
    'missing', custom, true),
  unverifiedPreserved: picker.reconcileOrchestrationFlowSelection(
    'missing', custom, false),
  noticeShown,
  noticeText: notice.textContent,
  noticeState: notice.dataset.state,
  tabStops: rows.map((row) => row.tabIndex),
  focused: document.activeElement?.getAttribute('data-flow'),
  selected,
  escaped,
  icon: picker.orchestrationFlowPickerIcon('flow-a').startsWith('<svg'),
}));
"""
    run = subprocess.run(
        ["node", "-e", script, str(bundle)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert [item["flow"] for item in result["projected"]] == [
        "", "builtin:autopilot", "flow-a",
    ]
    assert result["projectedFrozen"] is True
    assert result["saved"] == [{
        "flow": "flow-a",
        "name": "Flow A",
        "desc": "t:toolbar.flowCustomDesc",
    }]
    assert result["savedFrozen"] is True
    assert result["display"] == "Flow A"
    assert result["staleDropped"] == ""
    assert result["unverifiedPreserved"] == "missing"
    assert result["noticeShown"] is True
    assert result["noticeText"] == "t:toolbar.flowCatalogFailed · offline"
    assert result["noticeState"] == "failed"
    assert result["tabStops"] == [-1, -1, 0]
    assert result["focused"] == "c"
    assert result["selected"] == ["c"]
    assert result["escaped"] == 1
    assert result["icon"] is True


_PLAN_DECISION_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const fs = require('fs');
const ts = require('typescript');
const compiled = ts.transpileModule(
  fs.readFileSync(process.argv[2], 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022,
                       module: ts.ModuleKind.CommonJS } },
).outputText;
const decisionModule = { exports: {} };
new Function('module', 'exports', 'require', compiled)(
  decisionModule, decisionModule.exports, () => ({}),
);

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!doctype html><body><main id="surface"><article data-turn-id="turn-plan"><div data-conversation-part="turn-blocks"></div><aside id="planDecisionMount" data-conversation-part="turn-plan-decision"></aside></article><div class="input-box"><textarea id="userInput"></textarea></div></main></body>',
  targets: [],
});
global.Element = window.Element;

(async () => {
  const composer = document.querySelector('.input-box');
  const decisionMount = document.getElementById('planDecisionMount');
  const input = document.getElementById('userInput');
  let continued = 0;
  let executionContext = '';
  let releaseExecution;
  const bar = decisionModule.exports.createPlanDecisionBar({
    onContinueDiscussion: () => { continued += 1; },
    onExecute: (_conversationId, _decision, contextMode) => {
      executionContext = contextMode;
      return new Promise((resolve) => { releaseExecution = resolve; });
    },
  });
  bar.activateConversation('conv-a');
  bar.render(decisionMount, 'conv-a', {
    sourceTurnId: 'turn-plan', planId: 'plan-a',
    sourceProjectionRevision: 7, pending: false,
  });

  const decisionRoot = document.querySelector('[data-plan-decision-bar="true"]');
  check('Decision surface stays inside its source plan turn',
    decisionRoot === decisionMount
      && decisionRoot.closest('[data-turn-id]')?.dataset.turnId === 'turn-plan');
  check('Ready plan keeps the original composer mounted and enabled',
    document.querySelector('.input-box') === composer && input.disabled === false);
  check('Decision surface exposes all three choices',
    decisionRoot.querySelectorAll('[data-plan-decision-action]').length === 3);

  decisionRoot.querySelector('[data-plan-decision-action="continue"]').click();
  check('Continue discussion does not consume the plan',
    continued === 1 && document.body.contains(decisionRoot));

  decisionRoot.querySelector('[data-plan-decision-action="fresh"]').click();
  check('Fresh execution sends the explicit fresh context choice',
    executionContext === 'fresh');
  check('Only decision actions lock while execution is being accepted',
    [...decisionRoot.querySelectorAll('button')].every((button) => button.disabled)
      && input.disabled === false);

  releaseExecution();
  await new Promise((resolve) => setImmediate(resolve));
  check('Decision actions recover after command settlement',
    [...decisionRoot.querySelectorAll('button')].every((button) => !button.disabled));

  bar.activateConversation(null);
  check('Clearing a decision empties only its Surface mount, not the composer',
    decisionRoot.hidden && decisionRoot.childElementCount === 0
      && document.body.contains(composer));
  report();
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


def test_ready_plan_keeps_composer_usable_and_locks_only_decision_actions():
    run_harness(
        PLAN_DECISION_BAR,
        _PLAN_DECISION_HARNESS,
        expect_pass=8,
        label="plan decision surface",
    )


_MANAGE_ROUTING_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!doctype html><body><div id="submenuAgentMode" class="toolbar-submenu open"><button id="agentModeToggle" class="submenu-trigger open" aria-expanded="true"></button></div></body>',
  targets: [process.argv[2]],
  globals: {
    debugLog: function(){},
    updateSubmenuCounts: function(){},
  },
});

/* The Studio stylesheet lives in the lazy features/orchestration chunk, so
 * the Manage entry must ride the feature-bridge stub on runtimeScope; the
 * lexical openOrchestration mounts the shell without ever loading that CSS.
 * The test view rebinds runtimeScope to the jsdom window while bare
 * cross-section identifiers resolve through the node global, so the stub
 * lives on window.openOrchestration and the lexical owner on the node global. */
let stubCalls = 0;
let lexicalCalls = 0;
let sheetCloses = 0;
const lexicalOwner = function(){ lexicalCalls += 1; return true; };
global.openOrchestration = lexicalOwner;
global.closeMobileSheet = window.closeMobileSheet = function(){ sheetCloses += 1; };
window.openOrchestration = function(){ stubCalls += 1; };

const dispatched = openOrchestrationFromAgentMode();
check('Manage rides the feature-bridge stub, not the lexical owner',
  stubCalls === 1 && lexicalCalls === 0);
check('Manage reports dispatch', dispatched === true);
check('Agent-mode menu closes before routing',
  !document.getElementById('submenuAgentMode').classList.contains('open'));
check('Mobile sheet closes before routing', sheetCloses === 1);

window.openOrchestration = lexicalOwner;
check('Identity collapse falls back to one lexical call',
  openOrchestrationFromAgentMode() === true && lexicalCalls === 1 && stubCalls === 1);

delete window.openOrchestration;
check('Missing stub falls back to the lexical owner',
  openOrchestrationFromAgentMode() === true && lexicalCalls === 2);

delete global.openOrchestration;
check('No owner at all fails closed', openOrchestrationFromAgentMode() === false);
report();
"""


def test_manage_button_routes_through_feature_bridge_stub():
    run_harness(
        TOOLBAR,
        _MANAGE_ROUTING_HARNESS,
        expect_pass=7,
        label="manage button feature-bridge routing",
    )
