"""A native browser socket recovers when a clean proxy close is followed by wake."""
from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.visual


def test_page_activation_reopens_cleanly_closed_push(browser, live_server):
    from playwright.sync_api import expect

    context = browser.new_context(service_workers='block')
    sockets = []

    def accept_socket(socket):
        sockets.append(socket)

        def receive(raw):
            frame = json.loads(raw)
            if frame.get('action') == 'ping':
                socket.send(json.dumps({'type': 'pong', 't': frame['t']}))

        socket.on_message(receive)

    context.route_web_socket('**/api/push*', accept_socket)
    page = context.new_page()
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        badge = page.locator('#netLatencyBadge')
        expect(badge).to_have_attribute('data-state', re.compile('^(good|ok|poor)$'), timeout=20000)
        assert len(sockets) == 1
        sockets[0].close(code=1000, reason='Forwarding connection ended')
        expect(badge).to_have_attribute('data-state', 'offline')
        page.evaluate("window.dispatchEvent(new Event('pageshow'))")
        expect(badge).to_have_attribute('data-state', re.compile('^(good|ok|poor)$'))
        assert len(sockets) == 2
        assert page.evaluate("performance.getEntriesByType('navigation').length") == 1
    finally:
        context.close()
