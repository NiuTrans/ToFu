"""Retained browser workers keep independent tab hints on one device."""

import pytest

from lib.browser._resolve import (
    clear_work_tab_cache, current_work_tab, remember_work_tab,
)
from lib.browser.tool_runtime import BrowserToolRuntime


pytestmark = pytest.mark.unit


def test_two_workers_on_one_device_do_not_switch_each_others_tab():
    clear_work_tab_cache()
    try:
        first = BrowserToolRuntime(owner_user_id='7', client_id='device',
                                   work_key='agent-one')
        second = BrowserToolRuntime(owner_user_id='7', client_id='device',
                                    work_key='agent-two')
        remember_work_tab(first.route_key, 11)
        remember_work_tab(second.route_key, 22)
        assert current_work_tab(first.route_key) == 11
        assert current_work_tab(second.route_key) == 22
        assert current_work_tab(('7', 'device')) is None
    finally:
        clear_work_tab_cache()


def test_work_context_changes_only_tab_hint_not_bridge_authority():
    calls = []

    def send(command, params, *, timeout, client_id, owner_user_id):
        calls.append((command, client_id, owner_user_id))
        return {'ok': True}, None

    runtime = BrowserToolRuntime(owner_user_id='7', client_id='device',
                                 work_key='agent-one', sender=send)
    runtime.send('page_state', {'tabId': 11})
    assert calls == [('page_state', 'device', '7')]
