"""Command timing chips render backend samples without a browser stopwatch."""
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]


@pytest.mark.parametrize('width', [1280, 390])
def test_command_elapsed_and_budget_are_backend_samples(page, tmp_path, width):
    from playwright.sync_api import expect
    from lib.storage import get_storage_client
    from lib.turn_lifecycle import append_settled_turn
    from tests.conftest import _dismiss_onboarding_modals

    conv_id = 'visual-command-sample-' + uuid4().hex[:12]
    rounds = [
        {'toolName': 'run_command', 'roundNum': 1, 'toolCallId': 'sample-up',
         'status': 'searching', 'query': 'synthetic command',
         'execStartTs': 1700000000000, 'execTimingAtMs': 1700000195000,
         'execElapsedMs': 195000},
        {'toolName': 'run_command', 'roundNum': 2, 'toolCallId': 'sample-left',
         'status': 'searching', 'query': 'synthetic budget command',
         'execStartTs': 1700000000000, 'execTimingAtMs': 1700000005000,
         'execElapsedMs': 5000, 'deadlineRemainingMs': 90000},
        {'toolName': 'run_command', 'roundNum': 3, 'toolCallId': 'sample-legacy',
         'status': 'searching', 'query': 'old command without a sample',
         'execStartTs': 1700000000000},
    ]
    client = get_storage_client(write=True)
    try:
        result = append_settled_turn(
            conv_id, command_id=conv_id, actor='assistant', user_id=1,
            projection={'content': 'Backend command sample evidence', 'toolRounds': rounds},
            conversation_defaults={'allowCreate': True, 'title': 'Command sample evidence',
                                   'createdAt': 1, 'settings': {}})
        page.set_viewport_size({'width': width, 'height': 900})
        page.reload(wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        page.locator(f'.conv-item[data-conv-id="{conv_id}"]').wait_for()
        page.evaluate('Date.now = () => 9999999999999')
        page.evaluate("id => window.TofuModules.resolveAction('loadConversation')(id)", conv_id)
        turn = page.locator(f'[data-turn-id="{result["turn"]["turnId"]}"]')
        expect(turn).to_contain_text('Backend command sample evidence')
        chips = turn.locator('.ptool-cmd-timer')
        expect(chips).to_have_count(2)
        texts = chips.all_text_contents()
        assert texts[0] == '3m15s'
        assert '1m30s' in texts[1]
        assert chips.nth(1).get_attribute('class').find('-over') < 0
        assert all(chips.nth(index).get_attribute('title') for index in range(2))
        page.wait_for_timeout(1300)
        assert chips.all_text_contents() == texts
        screenshot = tmp_path / f'command-server-samples-{width}.png'
        turn.screenshot(path=str(screenshot))
        print(f'Command timing screenshot: {screenshot}')
    finally:
        client.command('conversation.delete', {'conv_id': conv_id, 'user_id': 1},
                       f'{conv_id}:cleanup')
