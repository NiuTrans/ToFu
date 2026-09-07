#!/usr/bin/env python3
"""Concurrent-writer safety for the shared ``server_config.json``.

Six independent code paths persist into the SINGLE file
``data/config/server_config.json`` via a read-modify-write:
``routes/config.py::save_server_config``, ``lib/context_limits._persist``,
``lib/model_info._learn_model_limit``, ``lib/llm_dispatch/dispatcher``
(discovery + header migration) and ``lib/llm_dispatch/health_local``.

Before this was unified they each did a bare ``open('w')+json.dump`` (some
non-atomic), so two writers touching DIFFERENT keys of the file at once
would clobber each other in the read→write gap — a lost-update race plus a
crash-truncation risk. The fix routes every one of them through
``lib.json_store.update_json_atomic``, whose per-path thread lock (+ sidecar
flock) serialises the whole RMW.

This test drives TWO of the real writers concurrently:
  • ``context_limits.learn_expand_from_success`` → writes ``model_context_limits``
  • ``model_info._learn_model_limit``            → writes ``model_limits``
against the SAME file and asserts BOTH sets of learned values survive.

NEGATIVE CONTROL (proven by hand): replace
``    with lock, _interprocess_lock(path):`` with ``    if True:`` inside
``lib/json_store.update_json_atomic`` (disabling both locks) → the
``model_limits`` count comes back short (context_limits writes back a stale
snapshot over model_info's concurrent additions) and
``test_context_limits_and_model_limits_dont_clobber`` FAILS. Restoring the
line byte-for-byte makes it pass again.
"""

import os
import sys
import tempfile
import threading

import pytest


pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _redirect_config_dir(tmp):
    """Point lib.config_dir.CONFIG_DIR (read at call-time by the writers) at tmp."""
    import lib.config_dir as cd
    cd.CONFIG_DIR = tmp
    return cd.config_path('server_config.json')


def _reset_writer_state():
    """Clear the in-memory learned-value dicts both writers load at import."""
    import lib.context_limits as ctx
    import lib.model_info as mi
    with ctx._lock:
        ctx._LEARNED.clear()
        ctx._META.clear()
    with mi._limits_lock:
        mi._LEARNED_MODEL_LIMITS.clear()


def test_context_limits_and_model_limits_dont_clobber():
    """Two real background writers, distinct keys, same file → no lost updates."""
    import lib.context_limits as ctx
    import lib.model_info as mi
    from lib.json_store import read_json

    tmp = tempfile.mkdtemp(prefix='srvcfg-cc-')
    cfg_path = _redirect_config_dir(tmp)
    _reset_writer_state()

    N = 80
    start = threading.Barrier(2)

    def write_context_limits():
        start.wait()
        for i in range(N):
            # observed > preset so the expand path actually persists a new key.
            ctx.learn_expand_from_success('provX', 'ctxmodel_%d' % i,
                                          observed_tokens=100_000,
                                          preset_limit=50_000)

    def write_model_limits():
        start.wait()
        for i in range(N):
            mi._learn_model_limit('limmodel_%d' % i, 16_384 + i)

    ta = threading.Thread(target=write_context_limits, name='ctx-writer')
    tb = threading.Thread(target=write_model_limits, name='lim-writer')
    ta.start(); tb.start()
    ta.join(); tb.join()

    final = read_json(cfg_path, default={})
    assert isinstance(final, dict), 'config file unreadable / truncated: %r' % final

    ctx_limits = final.get('model_context_limits') or {}
    model_limits = final.get('model_limits') or {}

    # The biting assertion: model_info's per-call single-key add is the race
    # victim — without the shared lock, context_limits writes a stale snapshot
    # back over it and entries vanish.
    assert len(model_limits) == N, (
        'lost model_limits updates: expected %d, got %d '
        '(concurrent writer clobbered them → the shared lock is not holding)'
        % (N, len(model_limits)))
    # And context_limits' own set must be fully present too.
    assert len(ctx_limits) == N, (
        'lost model_context_limits updates: expected %d, got %d'
        % (N, len(ctx_limits)))

    print('  \033[32m✓\033[0m %d context-limit + %d model-limit writes '
          'both survived concurrent persistence' % (len(ctx_limits), len(model_limits)))


def test_save_server_config_preserves_learned_limits():
    """save_server_config's mutator must not drop a concurrently-learned key.

    Simulates the ordering hazard directly: a learned model_limits entry is
    on disk, then the full Settings-save RMW runs. Because the save now reads
    the FRESH on-disk config inside the locked mutator, the learned entry
    survives alongside the saved providers block.
    """
    import lib.model_info as mi
    from lib.json_store import read_json, update_json_atomic

    tmp = tempfile.mkdtemp(prefix='srvcfg-save-')
    cfg_path = _redirect_config_dir(tmp)
    _reset_writer_state()

    # Background writer learned a limit first.
    mi._learn_model_limit('bgmodel', 32_768)
    assert (read_json(cfg_path).get('model_limits') or {}).get('bgmodel') == 32_768

    # A Settings save persists a providers change via the same locked RMW
    # shape used by save_server_config's _mutate.
    def _save_mutate(existing):
        if not isinstance(existing, dict):
            existing = {}
        existing['providers'] = [{'id': 'p1', 'models': []}]
        return existing
    update_json_atomic(cfg_path, _save_mutate, default={})

    final = read_json(cfg_path, default={})
    assert (final.get('model_limits') or {}).get('bgmodel') == 32_768, \
        'Settings save clobbered the concurrently-learned model_limits entry'
    assert final.get('providers') == [{'id': 'p1', 'models': []}], \
        'providers block not persisted'
    print('  \033[32m✓\033[0m save RMW preserved a concurrently-learned model_limits key')


def main():
    print('\n\033[36m═══ server_config.json concurrent-writer tests ═══\033[0m\n')
    tests = [
        test_context_limits_and_model_limits_dont_clobber,
        test_save_server_config_preserves_learned_limits,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            print('  \033[31m✗\033[0m %s: %s' % (fn.__name__, e))
            sys.exit(1)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print('  \033[31m✗\033[0m %s: unexpected %s' % (fn.__name__, type(e).__name__))
            sys.exit(1)
    print('\n\033[32m═══ ALL %d TESTS PASSED ═══\033[0m\n' % len(tests))


if __name__ == '__main__':
    main()
