"""Built application → real common write → durable Turn → real HTTP inspection.

No renderer injection or mocked HTTP outcome. Only the lost undo ACK is fault
injected. The existing isolated application fixture owns storage and the server.
"""
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_task_event_delivery import owner as owner, _task, _discard
from tests.turn_lifecycle.test_file_effect_inspection import project as project, write

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]


@pytest.mark.parametrize('width', [1280, 390])
def test_file_publication_inspection_survives_browser_reload(page, project, monkeypatch, owner, tmp_path, width):
    from playwright.sync_api import expect
    from lib.storage import get_storage_client
    from lib import turn_lifecycle
    from lib.project_mod.write_tools import _ops
    from tests.conftest import _dismiss_onboarding_modals

    client = get_storage_client(write=True)
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    _, task = _task(turn_lifecycle, SimpleNamespace(client=client), 'browser-file-effect')
    def lost_ack(*a, **kw):
        raise OSError('injected lost undo acknowledgement after file replacement')
    monkeypatch.setattr(_ops, '_record_modification_checked', lost_ack)
    calls = []
    page.on('request', lambda request: calls.append((request.method, request.url))
            if '/tool-effects/' in request.url else None)
    try:
        row, result = write(task, project, append=True)
        assert (project / 'new.txt').read_text() == 'hello\n', result
        operation_id = row['executionEffect']['operationId']
        page.set_viewport_size({'width': width, 'height': 1000})
        page.reload(wait_until='domcontentloaded'); _dismiss_onboarding_modals(page)
        snapshot = page.request.get(page.url.split('#', 1)[0].rstrip('/') + f'/api/v4/conversations/{task["convId"]}/sync')
        assert snapshot.status == 200, snapshot.text()
        assert any(t['turnId'] == task['_turnId'] for t in snapshot.json()['turns'])
        page.locator(f'.conv-item[data-conv-id="{task["convId"]}"]').wait_for()
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation') === 'function'")
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", task['convId'])
        turn = page.locator(f'[data-turn-id="{task["_turnId"]}"]')
        notice = turn.locator('[data-tool-effect-notice]')
        expect(notice).to_be_visible(timeout=15000)
        notice.locator('[data-tool-effect-inspect]').click()
        expect(notice).to_have_attribute('data-tool-effect-outcome', 'file_published')
        assert len(calls) == 1 and calls[0][0] == 'GET' and operation_id in calls[0][1]
        assert (project / 'new.txt').read_text() == 'hello\n'
        page.reload(wait_until='domcontentloaded'); _dismiss_onboarding_modals(page)
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", task['convId'])
        expect(notice).to_be_visible()
        # Query results are fresh DOM observations, never a stored acceptance.
        expect(notice).to_have_attribute('data-tool-effect-outcome', 'unknown')
        assert len(calls) == 1
        (project / 'new.txt').write_text('newer human content\n')
        notice.locator('[data-tool-effect-inspect]').click()
        expect(notice).to_have_attribute('aria-busy', 'false')
        expect(notice).to_have_attribute('data-tool-effect-outcome', 'unknown')
        assert len(calls) == 2 and all(method == 'GET' for method, _ in calls)
        assert (project / 'new.txt').read_text() == 'newer human content\n'
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path / f'file-effect-application-{width}.png'), full_page=True)
    finally:
        _discard(task)
        client.command('conversation.delete', {'conv_id': task['convId'], 'user_id': 1}, task['id'] + '-cleanup')
