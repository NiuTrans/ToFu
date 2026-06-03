"""tests/test_log_noise_reduction.py — 2026-05-05 log-noise audit.

Regression tests for the fixes in the log-noise reduction pass:

1. ``_parse_rerank_response`` tolerates preamble + code fences.
2. ``inject_relevant_memories(conv_id=...)`` calls
   ``cache_tracking.notify_compaction(conv_id)`` so the next
   ``detect_cache_break`` does NOT false-positive as a PREFIX MUTATION.
3. ``UnknownWorkspaceRootError`` is raised by ``resolve_namespaced_path``
   for unknown root names and is a ``ValueError`` subclass (backward-compat).
4. Benign 409 detection in ``server._is_benign_409``.
"""

from __future__ import annotations

import pytest


# ═════════════════════════════════════════════════════
# 1. Robust rerank JSON parser
# ═════════════════════════════════════════════════════

def test_parse_rerank_response_plain_json():
    from lib.memory.prefetch import _parse_rerank_response
    assert _parse_rerank_response('{"ids":[1,2],"reason":"x"}', max_idx=5) == [0, 1]


def test_parse_rerank_response_fenced_json():
    from lib.memory.prefetch import _parse_rerank_response
    txt = '```json\n{"ids":[1,2],"reason":"x"}\n```'
    assert _parse_rerank_response(txt, max_idx=5) == [0, 1]


def test_parse_rerank_response_with_leading_prose():
    """Previously this produced 30+ warnings/day: model prefixes with text."""
    from lib.memory.prefetch import _parse_rerank_response
    txt = 'Here is the answer:\n```json\n{"ids":[1,2],"reason":"x"}\n```'
    assert _parse_rerank_response(txt, max_idx=5) == [0, 1]


def test_parse_rerank_response_with_trailing_prose():
    from lib.memory.prefetch import _parse_rerank_response
    txt = '```json\n{"ids":[3],"reason":"y"}\n```\n\nHope this helps.'
    assert _parse_rerank_response(txt, max_idx=5) == [2]


def test_parse_rerank_response_balanced_braces_in_strings():
    """Brace counter must respect strings so `{"k":"}"}` parses correctly."""
    from lib.memory.prefetch import _parse_rerank_response
    # Extra prose + JSON with brace-like chars inside a string
    txt = 'here you go: {"ids":[1], "reason":"note: uses }{ chars"}'
    assert _parse_rerank_response(txt, max_idx=5) == [0]


def test_parse_rerank_response_garbage_returns_empty():
    from lib.memory.prefetch import _parse_rerank_response
    assert _parse_rerank_response('not json at all', max_idx=5) == []


# ═════════════════════════════════════════════════════
# 2. Memory-prefetch injection notifies cache_tracking
# ═════════════════════════════════════════════════════

def test_inject_relevant_memories_notifies_compaction(monkeypatch):
    """inject_relevant_memories(conv_id=...) must call notify_compaction
    so detect_cache_break doesn't false-positive as PREFIX MUTATION."""
    from lib.memory import prefetch as mp

    called = []

    def _fake_notify(conv_id):
        called.append(conv_id)

    monkeypatch.setattr('lib.tasks_pkg.cache_tracking.notify_compaction',
                        _fake_notify)

    messages = [{'role': 'user', 'content': 'Hi'}]
    mp.inject_relevant_memories(
        messages,
        [{'name': 'x', 'description': 'y', 'body': 'z'}],
        conv_id='conv-abc123',
    )
    assert called == ['conv-abc123']


def test_inject_relevant_memories_skips_notify_without_conv_id(monkeypatch):
    from lib.memory import prefetch as mp

    called = []
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking.notify_compaction',
                        lambda cid: called.append(cid))

    messages = [{'role': 'user', 'content': 'Hi'}]
    mp.inject_relevant_memories(
        messages,
        [{'name': 'x', 'description': 'y', 'body': 'z'}],
        # no conv_id
    )
    assert called == []


# ═════════════════════════════════════════════════════
# 3. UnknownWorkspaceRootError
# ═════════════════════════════════════════════════════

def test_unknown_workspace_root_error_is_value_error():
    from lib.project_mod.config import UnknownWorkspaceRootError
    assert issubclass(UnknownWorkspaceRootError, ValueError)


def test_unknown_workspace_root_error_raised(tmp_path):
    from lib.project_mod import config
    from lib.project_mod.config import UnknownWorkspaceRootError

    # Fresh per-conv registry with only one known root
    conv_id = 'test-conv-xyz'
    config.set_conv_roots(conv_id, str(tmp_path))

    with pytest.raises(UnknownWorkspaceRootError):
        config.resolve_namespaced_path('NOT_A_ROOT:foo.py', conv_id=conv_id)

    # Clean up
    config.clear_conv_state(conv_id)


def test_resolve_base_ignores_json_blob_in_path(tmp_path):
    """A stringified reads array stuffed into 'path' must NOT be parsed as a
    'rootname:path' spec (it contains a JSON ':' before char 40).  The
    prefix '[{"path"' is not a valid root name, so it falls through to the
    primary base_path instead of raising UnknownWorkspaceRootError."""
    from lib.project_mod.tools import _resolve_base

    blob = '[{"path": "lib/self_update.py", "start_line": 392, "end_line": 432]'
    base, rel = _resolve_base(str(tmp_path), blob)
    # Guard kicks in: prefix has '[{"' → not treated as a root name.
    assert base == str(tmp_path)
    assert rel == blob


def test_resolve_base_still_honors_real_root_prefix(tmp_path):
    """Genuine 'rootname:path' must still resolve via the registry."""
    from lib.project_mod import config
    from lib.project_mod.tools import _resolve_base

    conv_id = 'test-conv-rootprefix'
    config.set_conv_roots(conv_id, str(tmp_path))
    root_name = tmp_path.name
    base, rel = _resolve_base(str(tmp_path), f'{root_name}:sub/file.py', conv_id=conv_id)
    assert base == str(tmp_path)
    assert rel == 'sub/file.py'
    config.clear_conv_state(conv_id)


# ═════════════════════════════════════════════════════
# 4. Benign 409 detection
# ═════════════════════════════════════════════════════

def test_is_benign_409_recognises_regression_errors():
    """The lifecycle helper demotes our own guard 409s to INFO."""
    import json

    # Minimal response stub with the bits _is_benign_409 reads
    class _Resp:
        is_json = True
        def __init__(self, body):
            self._body = body
        def get_json(self, silent=True):
            try:
                return json.loads(self._body)
            except Exception:
                return None

    # Delay import until needed so the test module itself imports cleanly
    # on environments without flask wiring.
    try:
        from server import _is_benign_409
    except Exception:
        pytest.skip('server module not importable in this test env')

    assert _is_benign_409(_Resp('{"ok":false,"error":"blocked_msg_regression"}'))
    assert _is_benign_409(_Resp('{"ok":false,"error":"blocked_empty_overwrite"}'))
    assert _is_benign_409(_Resp('{"ok":false,"error":"blocked_stale_checkpoint"}'))
    assert not _is_benign_409(_Resp('{"ok":false,"error":"task_busy"}'))
    assert not _is_benign_409(_Resp('garbage'))
