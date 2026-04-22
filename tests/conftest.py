"""Shared pytest fixtures for the Tofu test suite.

Provides the ``flask_client`` fixture consumed by ``tests/test_api_integration.py``
and ``tests/test_conversation_search.py``.

Design:
  * Each test session gets a fresh, isolated SQLite database via ``CHATUI_DB_PATH``
    pointing at a temp file — no PostgreSQL required, no cross-test contamination.
  * The Flask ``app`` is imported lazily AFTER env-vars are set so
    ``lib.database._core`` picks the right backend at import time.
  * ``flask_client`` is function-scoped so each test gets a clean test client
    with its own cookie jar.
"""

from __future__ import annotations

import os
import tempfile

import pytest


# ─── Session-level: one SQLite DB per pytest run ──────────────────────
@pytest.fixture(scope="session", autouse=True)
def _configure_test_env():
    """Set env vars BEFORE importing the Flask app so the DB layer picks
    SQLite and isolates data to a temp file. Trading features are disabled
    to keep the surface area small.
    """
    tmpdir = tempfile.mkdtemp(prefix="tofu-test-")
    db_path = os.path.join(tmpdir, "chatui-test.db")

    os.environ.setdefault("CHATUI_DB_BACKEND", "sqlite")
    os.environ.setdefault("CHATUI_DB_PATH", db_path)
    os.environ.setdefault("TRADING_ENABLED", "0")
    os.environ.setdefault("PPTX_TRANSLATE_ENABLED", "0")
    # Avoid accidental real LLM calls in CI.
    os.environ.setdefault("LLM_API_KEY", "test-key-placeholder")
    os.environ.setdefault("LLM_API_KEYS", "test-key-placeholder")

    yield

    # Best-effort cleanup — don't fail the run if files are still locked.
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass


# ─── Session-level: build the Flask app once ──────────────────────────
@pytest.fixture(scope="session")
def flask_app(_configure_test_env):
    """Import and return the Flask app AFTER env-vars are set.

    This runs exactly once per test session so importing 800+ modules
    (server.py's full blueprint stack) doesn't dominate the wall-clock.
    """
    import server  # noqa: F401 — importing triggers app construction
    from server import app

    app.config.update(
        TESTING=True,
        # Disable Flask-Compress in tests — it can chunk responses in ways
        # the Werkzeug test client doesn't fully assemble.
        COMPRESS_REGISTER=False,
    )
    return app


# ─── Function-level: fresh test client per test ───────────────────────
@pytest.fixture()
def flask_client(flask_app):
    """Return a Werkzeug test client with its own cookie jar.

    Used by API integration tests and conversation-search tests.
    """
    with flask_app.test_client() as client:
        yield client
