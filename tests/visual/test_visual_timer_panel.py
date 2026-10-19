"""Real-browser check of the timer panel's two-tier card layout.

jsdom covers the string structure of ``_renderTimerList``; this tier proves
the panel survives the REAL bundle + real CSS + a real ``/api/v1/timer/list``
round trip: the badge un-hides, the panel opens through its production entry
point, and each card shows the human headline tier (instruction title, poll
outcome, compact progress) with raw record fields tucked behind <details>.
"""

from __future__ import annotations

import os

import pytest

from tests.visual.test_visual_surfaces import (
    _drain,
    _hard_errors,
    _invoke_action,
    _wait_ready,
)


pytestmark = [pytest.mark.visual, pytest.mark.slow]

_USER = 1
_SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), '..', 'screenshots')

_ACTIVE_INSTRUCTION = 'Check GitHub Actions Build Desktop run 34147441867'
_ACTIVE_REASON = "Job 'Windows Installer' failed with conclusion 'failure'."
_TRIGGERED_INSTRUCTION = (
    'Check hope job psxlskqxrr7ko5yn (TB2.1 cluster smoke, '
    'app_id psxlskqxrr7ko5yn) via hope'
)
_TRIGGERED_REASON = 'Job status is FAILED (terminal state).'


@pytest.fixture()
def seeded_timers(page):
    """Seed one active + one triggered watcher in the live app's store.

    A 3600s interval keeps the scheduler loop from polling mid-test; the
    triggered row is projected directly via the storage authority's
    ``timer.update`` (the same command the poll loop uses). Cleanup removes
    both rows so no other visual test inherits them.
    """
    from lib.scheduler import timer as timer_mod
    from lib.scheduler.timer._crud import _timer_client

    active = timer_mod.create_timer(
        user_id=_USER, conv_id='visual_timer_conv',
        check_instruction=_ACTIVE_INSTRUCTION,
        continuation_message='desktop build watcher fired',
        poll_interval=3600, max_polls=30,
        check_command='gh run view 34147441867')
    triggered = timer_mod.create_timer(
        user_id=_USER, conv_id='visual_timer_conv',
        check_instruction=_TRIGGERED_INSTRUCTION,
        continuation_message='cluster smoke watcher fired',
        poll_interval=3600, max_polls=120)
    now = '2026-09-09T23:23:49'
    _timer_client(write=True).command('timer.update', {
        'timer_id': active['id'], 'user_id': _USER,
        'poll_count': 3, 'last_poll_at': '2026-09-09T01:32:29',
        'last_poll_decision': 'wait', 'last_poll_reason': _ACTIVE_REASON,
        'updated_at': now,
    }, f'test:visual:timer:active:{active["id"]}')
    _timer_client(write=True).command('timer.update', {
        'timer_id': triggered['id'], 'user_id': _USER,
        'status': 'triggered', 'poll_count': 80,
        'last_poll_at': '2026-09-09T23:23:48',
        'last_poll_decision': 'ready', 'last_poll_reason': _TRIGGERED_REASON,
        'triggered_at': now, 'updated_at': now,
    }, f'test:visual:timer:triggered:{triggered["id"]}')

    yield {'active': active, 'triggered': triggered}

    timer_mod.cancel_timer(active['id'], user_id=_USER)
    _timer_client(write=True).command('timer.update', {
        'timer_id': triggered['id'], 'user_id': _USER,
        'status': 'cancelled', 'cancelled_at': now, 'updated_at': now,
    }, f'test:visual:timer:cleanup:{triggered["id"]}')


def test_timer_panel_renders_human_cards(page, seeded_timers):
    _wait_ready(page)
    _drain(page)
    _invoke_action(page, 'toggleTimerPanel')
    page.wait_for_selector('#timerPanel.visible .timer-panel-item',
                           state='visible', timeout=10000)
    page.wait_for_timeout(400)

    errs = _hard_errors(page)
    assert not errs, (
        'opening the timer panel raised JavaScript errors:\n  %s'
        % '\n  '.join(errs[:10]))

    cards = page.query_selector_all('#timerPanelContent .timer-panel-item')
    assert len(cards) == 2, f'expected 2 timer cards, found {len(cards)}'

    panel_text = page.inner_text('#timerPanelContent')
    # Headline tier: instruction is the title, poll outcome is the lead line.
    assert _TRIGGERED_REASON in panel_text
    assert _ACTIVE_REASON in panel_text
    assert '80 / 120' in panel_text and '3 / 30' in panel_text
    # Debugging tier is collapsed by default but present in the DOM.
    details = page.query_selector_all('#timerPanelContent .tpi-details')
    assert len(details) == 2
    assert all(not d.get_attribute('open') for d in details)
    assert seeded_timers['triggered']['id'] in page.inner_html(
        '#timerPanelContent')
    # The active card renders its poll budget as a progress bar.
    bar = page.query_selector(
        '#timerPanelContent .timer-status-active .tpi-progress-bar > div')
    assert bar is not None and 'width' in (bar.get_attribute('style') or '')

    os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
    shot = os.path.join(_SCREENSHOT_DIR, 'timer_panel_cards.png')
    page.locator('#timerPanel').screenshot(path=shot)
    print(f'\n  timer panel screenshot: {shot}')


def test_timer_panel_distinguishes_ready_dispatch_and_code_evidence(page, seeded_timers):
    from lib.scheduler import timer as timer_mod
    from lib.scheduler.timer._crud import _timer_client
    code = timer_mod.create_timer(user_id=_USER, conv_id='visual_timer_conv',
        check_instruction='', condition_command='test -f result.ready',
        continuation_message='Read the completed result', poll_interval=3600, max_polls=1)
    client = _timer_client(write=True)
    client.command('timer.update', {'timer_id':code['id'], 'user_id':_USER,
        'poll_count':1, 'last_poll_decision':'ready', 'last_poll_reason':'predicate matched (exit=0)'},
        f'test:visual:timer:ready:{code["id"]}')
    client.command('timer.update', {'timer_id':seeded_timers['triggered']['id'], 'user_id':_USER,
        'status':'failed', 'last_poll_decision':'dispatch_failed', 'last_poll_reason':'Target is unavailable'},
        f'test:visual:timer:failed:{seeded_timers["triggered"]["id"]}')
    try:
        _wait_ready(page)
        _drain(page)
        _invoke_action(page, 'toggleTimerPanel')
        card = page.locator('#timerPanelContent .timer-panel-item').filter(has_text=code['id'])
        card.wait_for(state='visible')
        assert card.locator('.tpi-title').inner_text() == 'test -f result.ready'
        assert card.locator('.tpi-status-label').inner_text() in ('Ready — waiting to start', '已就绪，等待启动')
        assert 'command-based' in card.inner_text() or '命令' in card.inner_text()
        assert card.locator('.tpi-detail-rows').text_content().find('Read the completed result') >= 0
        failed = page.locator('#timerPanelContent .timer-status-failed')
        assert failed.locator('.tpi-status-label').inner_text() in ('Failed', '启动失败')
        assert not _hard_errors(page)
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        page.locator('#timerPanel').screenshot(path=os.path.join(_SCREENSHOT_DIR, 'timer_ready_dispatch.png'))
        page.set_viewport_size({'width':420,'height':860})
        _invoke_action(page, 'openMobileTimer')
        card.wait_for(state='visible')
        assert card.is_visible()
        panel_box = page.locator('#timerPanel').bounding_box()
        assert panel_box and panel_box['width'] <= 420
        page.locator('#timerPanel').screenshot(path=os.path.join(_SCREENSHOT_DIR, 'timer_ready_dispatch_mobile.png'))
    finally:
        timer_mod.cancel_timer(code['id'], user_id=_USER)

def test_cancel_reconciles_after_older_list_response(page, seeded_timers):
    """Real POST cancellation must survive a delayed pre-cancel GET snapshot."""
    from playwright.sync_api import expect

    timer_id = seeded_timers['active']['id']
    _wait_ready(page)
    _drain(page)
    _invoke_action(page, 'toggleTimerPanel')
    card = page.locator('#timerPanelContent .timer-panel-item').filter(has_text=timer_id)
    expect(card.locator('.tpi-btn-cancel')).to_be_visible()
    held = []

    def delay_first_full_list(route):
        if not held and 'summary=1' not in route.request.url:
            response = route.fetch()
            held.append((route, response.json()))
            page.evaluate('window.__timerSnapshotHeld = true')
        else:
            route.continue_()

    page.route('**/api/v4/timer/list*', delay_first_full_list)
    try:
        # Reopening requests a full snapshot while retaining the visible row.
        _invoke_action(page, 'toggleTimerPanel')
        with page.expect_request('**/api/v4/timer/list*'):
            _invoke_action(page, 'toggleTimerPanel')
        expect(card.locator('.tpi-btn-cancel')).to_be_visible()
        page.wait_for_function('window.__timerSnapshotHeld === true')
        with page.expect_response(lambda response: response.url.endswith(f'/{timer_id}/cancel')) as mutation:
            card.locator('.tpi-btn-cancel').click()
        assert mutation.value.ok
        expect(card.locator('.tpi-pending')).to_be_visible()
        assert any(row['id'] == timer_id and row['status'] == 'active'
                   for row in held[0][1]['data']['timers'])
        held[0][0].fulfill(json=held[0][1])
        expect(card).to_have_class('timer-panel-item timer-status-cancelled')
        expect(card.locator('.tpi-pending')).to_have_count(0)
        expect(card.locator('.tpi-btn-cancel')).to_have_count(0)
        assert not _hard_errors(page)
    finally:
        page.unroute('**/api/v4/timer/list*', delay_first_full_list)
