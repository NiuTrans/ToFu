"""Office live-floor/gate/budget surfaces render in the fixture preview."""
from __future__ import annotations

import socket
import subprocess
import time

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]

ROOT = __file__.rsplit('/tests/', 1)[0]


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_office_floor_proposals_budget_render_in_fixture_preview(page):
    port = _free_port()
    server = subprocess.Popen(
        ['node', 'node_modules/vite/bin/vite.js', '--config',
         'frontend/src/features/evolution/testing/vite.config.mjs',
         '--host', '127.0.0.1', '--port', str(port), '--strictPort'],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f'http://127.0.0.1:{port}/preview.html'
        for _ in range(60):
            try:
                page.goto(url, timeout=2000)
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise AssertionError('preview server did not come up')
        page.wait_for_selector('.evo-gate-button', state='visible',
                               timeout=15000)
        gate = page.locator('.evo-gate-button')
        assert '2' in gate.text_content()
        assert page.locator('.evo-budget').count() == 1
        assert page.locator('.evo-budget-bar').count() == 1
        gate.click()
        page.wait_for_selector('.evo-proposal-row', timeout=5000)
        assert page.locator('.evo-proposal-row').count() == 2
        assert page.locator(
            '[data-studio-action="proposal-enable"]').count() == 2
        page.keyboard.press('Escape')
        page.locator('.evo-floor-button').click()
        page.wait_for_selector('.evo-floor-row', timeout=5000)
        kinds = page.eval_on_selector_all(
            '.evo-floor-row', 'rows => rows.map(r => r.dataset.kind)')
        assert kinds == ['evolution', 'flow', 'goal'], kinds
        assert page.locator('[data-studio-action="open-conv"]').count() == 1
        page.screenshot(path='output/office-surfaces-check.png',
                        full_page=False)
    finally:
        server.terminate()
        server.wait(timeout=10)
