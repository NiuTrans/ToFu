"""NON-MOCKED integration test for the killed-turn re-dispatch config chain.

WHY THIS FILE EXISTS (the reviewer's exact objection, and it is correct):
the unit tests in test_killed_recovery.py replace ``_redispatch_conv`` with a
lambda and call ``resolve_conv_config`` in isolation. That mocked shape is
PRECISELY what let the first shipped build look green while all 6 real
re-dispatches FATALed in production with
``'<' not supported between instances of 'int' and 'NoneType'``. A mocked test
of the broken path proves nothing.

This test drives the REAL chain that crashed — nothing in the config pipeline
is mocked:

    _redispatch_conv(conv)                 # REAL — reads a seeded DB row
      → resolve_conv_config(no overrides)  # REAL — the None-maxTokens source
      → create_task(...)                   # REAL — builds task['config']
      → _resolve_model_config(config)      # REAL — the .get() default trap
      → build_body(model, msgs, max_tokens)# REAL — where _clamp_max_tokens fired

Only the two true boundaries are stubbed: ``spawn_task`` (don't actually run
the async orchestrator / hit the LLM network) is replaced with a capture, and
the LLM network itself is never reached because we stop at build_body. The
assertion is exactly what would have caught the incident: the config the real
recovery path produces resolves to a POSITIVE INTEGER max_tokens and
build_body constructs a request WITHOUT raising.

Self-contained: seeds a throwaway SQLite DB via TOFU_DB_BACKEND=sqlite +
TOFU_DB_PATH so it needs no live PG / server (the env pytest is also poisoned
by a napari/vispy GL import at collection — run standalone:
``python3 tests/test_killed_recovery_integration.py``).
"""

import importlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_SEED_N = [0]


def _seed_sqlite_conv(model_in_settings, with_max_tokens=False):
    """Create a throwaway SQLite DB with one conversation whose tail is killed.

    Returns (conv_id, db_dir) — the caller keeps db_dir alive for the test.
    """
    db_dir = tempfile.mkdtemp(prefix='tofu_kr_it_')
    os.environ['TOFU_DB_BACKEND'] = 'sqlite'
    os.environ['TOFU_DB_PATH'] = os.path.join(db_dir, 'tofu.db')

    # Reload the DB layer so it binds the temp path + sqlite backend.
    import lib.database._core as _core
    importlib.reload(_core)
    import lib.database as _db
    importlib.reload(_db)
    _db.init_db()

    # Distinct conv id per seed: the first create_task registers a running task
    # for this conv in the shared _chat_runtime, so a reused id would make the
    # next _conv_has_live_task() short-circuit (skip) — a harness artefact, not
    # a product bug.
    _SEED_N[0] += 1
    conv_id = 'kritconv%04d' % _SEED_N[0]
    settings = {'model': model_in_settings}
    if with_max_tokens:
        settings['maxTokens'] = 64000
    messages = [
        {'role': 'user', 'content': 'ping', '_msgId': 'u-int-1'},
        {'role': 'assistant', 'content': 'partial…',
         'finishReason': 'interrupted', 'interruptedReason': 'killed'},
    ]
    db = _db.get_thread_db(_db.DOMAIN_CHAT)
    db.execute(
        'INSERT INTO conversations (id, user_id, title, messages, settings, '
        'created_at, updated_at) VALUES (?,1,?,?,?,?,?)',
        (conv_id, 'kr-it', json.dumps(messages), json.dumps(settings),
         0, 0))
    db.commit()
    return conv_id, db_dir


def _drive_real_chain(conv_id):
    """Run the REAL _redispatch_conv, capturing the task; then run the REAL
    _resolve_model_config + build_body on the config it produced.

    Returns the constructed request body (dict). Raises if any real step in the
    chain raises — which is exactly the production FATAL we are guarding.
    """
    import lib.tasks_pkg.killed_recovery as kr
    importlib.reload(kr)

    captured = {}

    # Stub ONLY the execution boundary: spawn_task would launch the async
    # orchestrator (and eventually hit the LLM network). We capture the task
    # instead and then exercise the config chain synchronously ourselves.
    import lib.tasks_pkg as tasks_pkg

    def _capture_spawn(task):
        captured['task'] = task

    real_spawn = getattr(tasks_pkg, 'spawn_task', None)
    tasks_pkg.spawn_task = _capture_spawn
    # killed_recovery imports spawn_task lazily from lib.tasks_pkg, so patching
    # the package attribute is sufficient.
    try:
        # REAL _redispatch_conv — reads the seeded row, resolves config, builds
        # the task via the REAL create_task. No mock of _redispatch_conv.
        new_tid = kr._redispatch_conv(conv_id)
    finally:
        if real_spawn is not None:
            tasks_pkg.spawn_task = real_spawn

    assert new_tid, 'redispatch returned no task id'
    task = captured.get('task')
    assert task is not None, 'spawn_task was not called — no task captured'
    cfg = task['config']

    # REAL _resolve_model_config — the .get('maxTokens', 128000) trap.
    from lib.tasks_pkg.model_config import _resolve_model_config
    mcfg = _resolve_model_config(cfg, task['id'])
    max_tokens = mcfg['max_tokens']
    assert isinstance(max_tokens, int) and max_tokens > 0, (
        'max_tokens resolved to %r (type %s) — the production FATAL was here'
        % (max_tokens, type(max_tokens).__name__))

    # REAL build_body — where _clamp_max_tokens(model, None) raised
    # "'<' not supported between instances of 'int' and 'NoneType'".
    from lib.llm.body import build_body
    body = build_body(mcfg['model'], task['messages'], max_tokens=max_tokens)
    assert isinstance(body.get('max_tokens'), int) and body['max_tokens'] > 0
    return body, max_tokens


def test_real_redispatch_chain_builds_valid_request_no_maxtokens_in_settings():
    """The incident case: settings carry a model but NO maxTokens.

    Before the fix this drove the whole real chain to a FATAL. Now it must
    build a valid request with a positive-int max_tokens.
    """
    conv_id, db_dir = _seed_sqlite_conv('aws.claude-opus-4.8', with_max_tokens=False)
    body, mt = _drive_real_chain(conv_id)
    assert mt > 0
    print('OK real chain (no maxTokens in settings): built body, max_tokens=%d' % mt)


def test_real_redispatch_chain_positive_int_for_non_claude_model():
    """A non-Claude model (different clamp ceiling) still yields a positive int.

    Guards that the fix is not Claude-specific — the recovery config resolves a
    valid max_tokens whatever the stored model is. NOTE: maxTokens is NOT a
    per-conv-settings field in resolve_conv_config (it reads overrides/
    server_defaults only), so the value comes from the server_defaults the
    recovery path injects (128000), then clamped to the model's ceiling. The
    load-bearing assertion is "positive int, no FATAL", not a specific number.
    """
    conv_id, db_dir = _seed_sqlite_conv('gpt-4o', with_max_tokens=False)
    body, mt = _drive_real_chain(conv_id)
    assert isinstance(mt, int) and mt > 0, mt
    # build_body's body max_tokens is positive too (it may be further reduced
    # by the context-window clamp — the point is it never becomes None/crashes).
    assert isinstance(body['max_tokens'], int) and body['max_tokens'] > 0
    print('OK real chain (gpt-4o): positive-int max_tokens=%d, no FATAL' % mt)


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            import traceback
            print('FAIL %s: %s' % (fn.__name__, e))
            traceback.print_exc()
    print('\n%d/%d passed' % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
