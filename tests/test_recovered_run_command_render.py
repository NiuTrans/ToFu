"""Crash-recovered run_command rounds keep the rich terminal rendering."""

from __future__ import annotations

import json
import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit


def _thin_segments_for(rounds):
    from lib.tasks_pkg.segments import assemble_segments, segments_to_json

    return segments_to_json(assemble_segments({
        'content': '',
        'thinking': '',
        'toolRounds': rounds,
    }))


def _recover_round(tool_name='run_command'):
    from lib.tasks_pkg.manager._recovery import _tool_rounds_from_task_row

    command = "python -m pytest tests/test_widget.py"
    description = "Run the widget regression test"
    tool_content = f"$ {command}\n1 passed\n[exit code: 0]"
    rounds = [{
        'roundNum': 1,
        'llmRound': 0,
        'toolName': tool_name,
        'toolCallId': f'tc_{tool_name}_1',
        'status': 'done',
        'toolContent': tool_content,
        'toolArgs': json.dumps({
            'command': command,
            'description': description,
            'timeout': 120,
            'working_dir': '',
        }),
    }]
    recovered = _tool_rounds_from_task_row({
        'tool_rounds': None,
        'segments': json.dumps(_thin_segments_for(rounds)),
    })
    return recovered[0], command, description


@pytest.mark.parametrize('tool_name', ['run_command', 'code_exec'])
def test_recovery_rebuilds_command_terminal_metadata(tool_name):
    recovered, command, description = _recover_round(tool_name)

    result = recovered['results'][0]
    assert result['recovered'] is True
    assert result['toolName'] == tool_name
    assert result['command'] == command
    assert result['description'] == description
    assert result['output'] == '1 passed'
    assert result['exitCode'] == '0'


def test_recovery_upgrades_old_generic_result_but_preserves_live_result():
    from lib.tasks_pkg.manager._recovery import _tool_rounds_from_task_row

    recovered, command, description = _recover_round()
    old_generic = {
        **recovered,
        'results': [{
            'toolName': 'run_command',
            'badge': 'done',
            'title': command,
            'snippet': '1 passed',
            'recovered': True,
        }],
    }
    upgraded = _tool_rounds_from_task_row({
        'tool_rounds': json.dumps([old_generic]),
        'segments': None,
    })[0]['results'][0]
    assert upgraded['command'] == command
    assert upgraded['description'] == description
    assert upgraded['output'] == '1 passed'
    assert upgraded['exitCode'] == '0'

    live = {
        **recovered,
        'results': [{
            'toolName': 'run_command',
            'command': command,
            'description': 'live metadata',
            'output': 'remote result without local markers',
            'exitCode': 0,
        }],
    }
    projected = _tool_rounds_from_task_row({
        'tool_rounds': json.dumps([live]),
        'segments': None,
    })
    assert projected == [live]


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[4], process.argv[2]],
  globals: {
    _convRenderFingerprint: () => 0,
    conversations: [],
    activeConvId: null,
  },
});

function render(round) {
  const root = document.createElement('div');
  root.innerHTML = renderToolRoundsHTML([round], false);
  return { root, html: root.innerHTML };
}

const backend = render(__BACKEND_ROUND__);
check('backend_recovery_uses_terminal_card', !!backend.root.querySelector('.ptool-cmd-block'));
check('backend_recovery_success_status', !!backend.root.querySelector('.ptool-cmd-ok'));
check('backend_recovery_keeps_description', backend.html.includes('Run the widget regression test'));
check('backend_recovery_keeps_command', backend.html.includes('tests/test_widget.py'));
check('backend_recovery_keeps_output', backend.html.includes('1 passed'));

const legacyCommand = "python - <<'PY'\nprint('legacy recovery')\nPY";
const legacyDescription = 'Run the recovered command regression';
const legacy = render({
  roundNum: 2,
  toolName: 'run_command',
  toolCallId: 'tc_legacy',
  status: 'done',
  query: legacyCommand,
  toolArgs: JSON.stringify({ command: legacyCommand, description: legacyDescription }),
  toolContent: `$ ${legacyCommand}\nlegacy recovery\n[exit code: 0]`,
  results: [{ toolName: 'run_command', badge: 'done', recovered: true }],
});
check('old_generic_recovery_uses_terminal_card', !!legacy.root.querySelector('.ptool-cmd-block'));
check('old_generic_recovery_keeps_description', legacy.html.includes(legacyDescription));
check('old_generic_recovery_success_status', !!legacy.root.querySelector('.ptool-cmd-ok'));

const remote = render({
  roundNum: 3,
  toolName: 'run_command',
  status: 'done',
  toolArgs: { command: 'remote-build', description: 'Run remote build' },
  toolContent: 'remote formatted response without local markers',
  results: [{ command: 'remote-build', description: 'Run remote build',
    output: 'remote ok', exitCode: 0 }],
});
check('structured_remote_exit_wins', !!remote.root.querySelector('.ptool-cmd-ok'));
check('structured_remote_not_misclassified', !remote.root.querySelector('.ptool-cmd-notrun'));

const timeout = render({
  roundNum: 4, toolName: 'run_command', status: 'done', query: 'slow',
  toolArgs: '{"command":"slow"}',
  toolContent: '$ slow\npartial\n[Command timed out] after 120s',
  results: [{ recovered: true }],
});
check('legacy_timeout_status', !!timeout.root.querySelector('.ptool-cmd-timeout'));

const interrupted = render({
  roundNum: 5, toolName: 'run_command', status: 'done', query: 'watch',
  toolArgs: '{"command":"watch"}',
  toolContent: '$ watch\npartial\n[Command interrupted by user]',
  results: [{ recovered: true }],
});
check('legacy_interrupted_status', !!interrupted.root.querySelector('.ptool-cmd-interrupted'));

const failed = render({
  roundNum: 6, toolName: 'run_command', status: 'done', query: 'false',
  toolArgs: '{"command":"false"}', toolContent: '$ false\nboom\n[exit code: 7]',
  results: [{ recovered: true }],
});
check('legacy_nonzero_status', !!failed.root.querySelector('.ptool-cmd-err'));

const refused = render({
  roundNum: 7, toolName: 'run_command', status: 'done', query: 'rm -rf /',
  toolArgs: '{"command":"rm -rf /"}', toolContent: 'Command blocked for safety',
  results: [{ recovered: true }],
});
check('legacy_notrun_status', !!refused.root.querySelector('.ptool-cmd-notrun'));
check('legacy_notrun_reason_visible', refused.html.includes('Command blocked for safety'));

const malformed = render({
  roundNum: 8, toolName: 'run_command', status: 'done', query: 'echo fallback',
  toolArgs: '{broken', toolContent: '$ echo fallback\nok\n[exit code: 0]',
  results: [{ recovered: true }],
});
check('malformed_args_still_terminal', !!malformed.root.querySelector('.ptool-cmd-block'));
check('malformed_args_uses_query', malformed.html.includes('echo fallback'));

const codeExec = render(__BACKEND_CODE_EXEC_ROUND__);
check('backend_code_exec_uses_terminal_card', !!codeExec.root.querySelector('.ptool-cmd-block'));

const unknown = render({
  roundNum: 9,
  toolName: 'unknown_tool',
  status: 'done',
  toolArgs: JSON.stringify({ command: 'echo should-not-infer' }),
  results: [{ recovered: true }],
});
check('unknown_tool_stays_generic', !!unknown.root.querySelector('.ptool-line') &&
  !unknown.root.querySelector('.ptool-cmd-block'));
report();
"""


def test_recovered_commands_render_terminal_cards():
    recovered, _, _ = _recover_round()
    code_exec, _, _ = _recover_round('code_exec')
    body = _BODY.replace(
        '__BACKEND_ROUND__', json.dumps(recovered, ensure_ascii=False),
    ).replace(
        '__BACKEND_CODE_EXEC_ROUND__', json.dumps(code_exec, ensure_ascii=False),
    )
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=body,
        extra_targets=[os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js')],
        expect_pass=19,
        label='recovered run_command render',
    )
