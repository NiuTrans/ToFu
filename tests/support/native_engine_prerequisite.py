"""A typed native-engine refusal skips unless the lane selected an engine.

Entry points: ``inherited_engine_refusal_skips`` wraps any native authority
start (the conftest setup/call hooks and the shared app fixture use it);
``require_native_engine_for_checkout`` checks before a child process starts
one. A lane that selected ``TOFU_TEST_TOFUDB_BIN`` keeps every refusal fatal
(docs/TESTING_STRATEGY.md, "Live-app fixture prerequisites").
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest


@contextlib.contextmanager
def inherited_engine_refusal_skips():
    from serverctl_pkg.native_engine import EnginePreparationError

    try:
        yield
    except EnginePreparationError as exc:
        if os.environ.get('TOFU_TEST_TOFUDB_BIN'):
            raise
        pytest.skip(
            'inherited native storage engine cannot serve this checkout: '
            'select one built from it with TOFU_TEST_TOFUDB_BIN '
            '(docs/STORAGE_OPERATIONS.md) or run the SQLite lane with '
            f'TOFU_PERSONAL_STORAGE_BACKEND=sqlite ({exc})')


def require_native_engine_for_checkout() -> None:
    """Admit the engine pair exactly as a native authority started here would."""
    from lib.storage.supervisor import (
        _resolve_tofudb_binary, _resolve_tofudb_embedded_engine)

    with inherited_engine_refusal_skips():
        binary = Path(_resolve_tofudb_binary())
        if (binary.parent / 'libtofu_db.so').is_file():
            _resolve_tofudb_embedded_engine()
