"""Shared pytest fixtures for the ChatUI test suite.

Provides:
  - flask_app / flask_client   — ChatUI Flask app with in-memory DB (no real LLM)
  - mock_llm_port / mock_llm   — Standalone mock LLM server on a random port
  - live_server                 — ChatUI running on a real port (for Playwright)
  - browser / page              — Playwright browser and page fixtures
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ═══════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════

def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, host: str = "127.0.0.1", timeout: float = 15.0):
    """Block until a TCP port is accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"Port {host}:{port} not reachable within {timeout}s")


# ═══════════════════════════════════════════════════════════
#  Fixture: Mock LLM API Server (standalone process)
# ═══════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def mock_llm_port():
    """Start a mock LLM server on a free port. Yields the port number."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(PROJECT_ROOT, "tests", "mock_llm_server.py"), str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port(port)
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ═══════════════════════════════════════════════════════════
#  Fixture: ChatUI Flask App (test client, in-memory DB)
# ═══════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def flask_app(mock_llm_port):
    """Create ChatUI Flask app configured to use mock LLM & temp DB."""
    # Override env BEFORE importing server modules
    os.environ["LLM_BASE_URL"] = f"http://127.0.0.1:{mock_llm_port}/v1"
    os.environ["LLM_API_KEYS"] = "mock-test-key"
    os.environ["LLM_API_KEY"] = "mock-test-key"
    os.environ["TRADING_ENABLED"] = "0"

    from lib.database import init_db
    from server import app

    app.config["TESTING"] = True

    # Init DB within app context
    with app.app_context():
        init_db()

    yield app


@pytest.fixture
def flask_client(flask_app):
    """Flask test client — no real HTTP, direct WSGI calls."""
    with flask_app.test_client() as client:
        with flask_app.app_context():
            yield client


# ═══════════════════════════════════════════════════════════
#  Fixture: Live server (for Playwright tests)
# ═══════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def live_server(flask_app, mock_llm_port):
    """Run the ChatUI Flask app on a real port for Playwright tests."""
    port = _free_port()

    def run():
        flask_app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_for_port(port)

    yield f"http://127.0.0.1:{port}"


# ═══════════════════════════════════════════════════════════
#  Fixture: Playwright browser + page
# ═══════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def browser():
    """Launch a headless Chromium browser via Playwright."""
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    yield browser
    browser.close()
    pw.stop()


@pytest.fixture
def page(browser, live_server):
    """Fresh browser page pointed at the live ChatUI server.

    Automatically cleans up any conversations created during the test
    so E2E tests don't pollute the production database.
    """
    import urllib.request

    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    pg = ctx.new_page()
    pg.goto(live_server, wait_until="networkidle")

    # Snapshot conversation IDs before the test
    conv_ids_before = set()
    try:
        before_json = pg.evaluate("conversations.map(c => c.id)")
        conv_ids_before = set(before_json or [])
    except Exception:
        pass

    yield pg

    # ── Cleanup: delete conversations created during the test ──
    # We must call deleteConversation() from *inside* the browser so
    # the frontend removes them from its in-memory array.  A server-side
    # DELETE alone is insufficient because the frontend periodically
    # syncs conversations back, re-creating anything still in memory.
    try:
        conv_ids_after = set(pg.evaluate("conversations.map(c => c.id)") or [])
    except Exception:
        conv_ids_after = set()

    new_conv_ids = conv_ids_after - conv_ids_before
    for cid in new_conv_ids:
        try:
            pg.evaluate(f"deleteConversation('{cid}')")
        except Exception:
            # Fallback: server-side delete if page is already closed
            try:
                req = urllib.request.Request(
                    f"{live_server}/api/conversations/{cid}",
                    method="DELETE",
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass

    if new_conv_ids:
        print(f"  🧹 Cleaned up {len(new_conv_ids)} test conversation(s): "
              f"{', '.join(sorted(new_conv_ids))}")

    pg.close()
    ctx.close()


# ═══════════════════════════════════════════════════════════
#  Fixture: Screenshot directory
# ═══════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def screenshot_dir():
    """Directory for test screenshots."""
    d = os.path.join(PROJECT_ROOT, "tests", "screenshots")
    os.makedirs(d, exist_ok=True)
    yield d
