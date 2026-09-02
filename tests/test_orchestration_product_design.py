"""Product contract for Debug-only workflows and Studio → chat handoff."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path, runtime_section_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent
USE_COMMAND = ROOT / "frontend/src/features/orchestration/workspace-use-command.ts"
MUTATION_READ = ROOT / "frontend/src/features/orchestration/definition-mutation-read.ts"
MCP_RUNTIME = runtime_section_path("settings/mcp.js")


def test_workflows_share_agent_mode_and_remain_debug_only():
    from lib.application_shell_fragments import (
        inject_fragments,
        list_fragment_names,
        marker_for,
    )

    # Exercise the fragment assembler as a public behavior. Shell-marker
    # placement/parity is owned by test_application_shell_fragments; this
    # product contract only owns the rendered workflow controls.
    assembled = inject_fragments(
        "\n".join(marker_for(name) for name in sorted(list_fragment_names()))
    )
    assert 'id="agentWorkflowSection" style="display:none"' in assembled
    assert 'id="flowMenuList"' in assembled
    assert 'id="submenuFlow"' not in assembled
    assert 'id="mobileWorkflowSection" style="display:none"' in assembled
    assert 'id="mobileFlowSection"' not in assembled


_DEBUG_VISIBILITY_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
let saves = 0;
const ids = [
  'studioTopbarBtn', 'tasksTopbarBtn', 'mobileStudio', 'mobileTasks',
  'agentWorkflowSection', 'mobileWorkflowSection',
];
const html = '<!doctype html><body>'
  + ids.map(id => `<button id="${id}" style="display:none"></button>`).join('')
  + '</body>';
const { document, check, report } = setup({
  root: process.argv[3],
  html,
  targets: [process.argv[2]],
  globals: {
    _featureFlags: { debug_mode: true },
    activeFlow: 'flow-a',
    _applyFlowUI: value => { global.activeFlow = window.activeFlow = value; },
    captureActiveConversationSettings: () => { saves += 1; },
  },
});
_applyDebugModeVisibility();
check('debug mode reveals every workflow entry',
  ids.every(id => document.getElementById(id).style.display === ''));
_featureFlags.debug_mode = false;
_applyDebugModeVisibility();
check('normal mode hides every workflow entry',
  ids.every(id => document.getElementById(id).style.display === 'none'));
check('hiding experimental workflows releases the next-turn owner',
  activeFlow === '' && saves === 1);
report();
"""


def test_debug_mode_visibility_owns_every_workflow_entry():
    run_harness(
        MCP_RUNTIME,
        _DEBUG_VISIBILITY_HARNESS,
        expect_pass=3,
        label="debug-only workflow visibility",
    )


_HANDOFF_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const fs = require('fs');
const { check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2]],
});

(async () => {
  let token = 'doc-a';
  let revision = 4;
  let currentId = null;
  let saveCalls = 0;
  let uses = [];
  let toasts = [];
  let releaseSave;
  const command = createOrchestrationWorkspaceUseCommand({
    save: () => {
      saveCalls += 1;
      return new Promise(resolve => { releaseSave = resolve; });
    },
    currentId: () => currentId,
    documentToken: () => token,
    revision: () => revision,
    useDefinition: async id => { uses.push(id); return true; },
    translate: key => key,
    toast: (message, error) => toasts.push({message, error}),
  });

  const first = command.saveAndUse();
  const duplicate = command.saveAndUse();
  currentId = 'flow-a';
  releaseSave({id:'flow-a'});
  check('duplicate clicks share one save and one handoff',
    first === duplicate && await first === true
      && saveCalls === 1 && uses.join(',') === 'flow-a');

  const stale = command.saveAndUse();
  revision = 5;
  releaseSave({id:'flow-a'});
  check('a newer draft is never selected from an older save',
    await stale === false && uses.length === 1
      && toasts.at(-1).message === 'orch.use.stale');

  const switched = command.saveAndUse();
  token = 'doc-b';
  releaseSave({id:'flow-a'});
  check('switching documents during save blocks the handoff',
    await switched === false && uses.length === 1
      && toasts.at(-1).message === 'orch.use.stale');

  let rejectedUses = 0;
  const rejected = createOrchestrationWorkspaceUseCommand({
    save: async () => ({id:'flow-b'}),
    currentId: () => 'flow-b',
    documentToken: () => 'doc-b',
    revision: () => 1,
    useDefinition: async () => { rejectedUses += 1; return false; },
    translate: key => key,
    toast: (message, error) => toasts.push({message, error}),
  });
  check('busy chat stays in Studio after save',
    await rejected.saveAndUse() === false && rejectedUses === 1
      && toasts.at(-1).message === 'orch.use.busy');

  let conflictUses = 0;
  const conflict = createOrchestrationWorkspaceUseCommand({
    save: async () => null,
    currentId: () => 'flow-c',
    documentToken: () => 'doc-c',
    revision: () => 1,
    useDefinition: async () => { conflictUses += 1; return true; },
  });
  check('failed or conflicting save never switches workflows',
    await conflict.saveAndUse() === false && conflictUses === 0);
  report();
})();
"""


@pytest.mark.skipif(not USE_COMMAND.is_file(), reason="owner unavailable")
def test_save_and_use_is_single_flight_and_editor_owned():
    bundled = native_module_path(
        "orchestration-workspace-use-command.js", USE_COMMAND
    )
    run_harness(
        bundled,
        _HANDOFF_HARNESS,
        expect_pass=5,
        label="Studio save-and-use command",
    )


_CAS_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2]],
});
const response = {
  ok:false,
  status:409,
  data:{
    ok:false,
    error:'stale definition',
    conflict:'stale_definition',
    currentUpdatedAt:99,
    write:{
      format:'tofu.orchestration.definition-write/v1',
      reason:'stale_definition',
      operation:'replace',
      expectedUpdatedAt:8,
      currentUpdatedAt:99,
    },
  },
};
const save = normalizeOrchestrationDefinitionSave(response);
check('non-negative CAS versions produce a recoverable conflict',
  save.ok === false && save.reason === 'write-conflict'
    && save.conflict.currentUpdatedAt === 99);
response.data.write.currentUpdatedAt = -1;
const malformed = normalizeOrchestrationDefinitionSave(response);
check('negative CAS versions fail closed as malformed',
  malformed.conflict === null && malformed.ok === false);
report();
"""


def test_definition_write_conflicts_accept_valid_version_tokens():
    bundled = native_module_path(
        "orchestration-definition-mutation-read.js", MUTATION_READ
    )
    run_harness(
        bundled,
        _CAS_HARNESS,
        expect_pass=2,
        label="orchestration CAS projection",
    )
