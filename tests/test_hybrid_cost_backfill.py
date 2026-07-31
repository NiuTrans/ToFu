"""tests/test_hybrid_cost_backfill.py — the APPLY path of the cost backfill.

★ WHY THIS FILE EXISTS
======================
``tests/_migrate_hybrid_cost_backfill.py`` rewrites persisted ``cost`` stamps
that were produced by the pre-``ebfd5464`` hybrid double-count. Its dry-run half
was exercised repeatedly; its ``--apply`` half had ZERO coverage and was DEAD
CODE — it imported ``save_conversation_messages`` from ``lib.conversations``,
where no such symbol exists, so the first real write would have raised
``ImportError`` before touching a single row.

That is the same failure shape this whole investigation kept hitting: the path
that is never driven rots silently while the paths around it stay green. A
migration script whose writer only breaks when it finally touches real data is
worse than no script.

These tests drive the REAL writer against a REAL conversation row, so:
  * the store API must actually exist and be callable;
  * cost must actually change on disk;
  * ``usage`` must survive byte-identical (it is the evidence the correction is
    derived from — losing it makes the migration unauditable and unrepeatable);
  * a second run must be a no-op (idempotence — a re-run after a partial apply
    must not double-apply or thrash);
  * a lost CAS race must be retried against a re-read transcript, never
    resolved by re-pushing the stale copy (that is the clobber the store's
    ``expected_rev`` exists to prevent).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_hybrid_cost_backfill.py -v
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _load_migration():
    """Import the migration by path (leading underscore = not auto-collected)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '_migrate_hybrid_cost_backfill.py')
    spec = importlib.util.spec_from_file_location('_bf_mig', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hybrid_round(uncached: int, read: int, write: int, tag: str) -> dict:
    """One apiRounds entry carrying the real sankuai_anthropic hybrid shape.

    ``prompt_tokens`` is the cache-INCLUSIVE total while ``input_tokens`` is the
    Anthropic residual — both present, which is what the pre-fix engine misread.
    The ``cost`` block is the WRONG one the old engine would have stamped: the
    whole prefix priced at the uncached rate.
    """
    total = uncached + read + write
    return {
        'tag': tag,
        'round': int(tag[1:]),
        'model': 'claude-opus-5',
        'usage': {
            'input_tokens': uncached,
            'output_tokens': 100,
            'completion_tokens': 100,
            'prompt_tokens': total,
            'total_tokens': total + 100,
            'cached_tokens': 0,
            'cache_read_tokens': 0,
            'cache_write_tokens': 0,
            'cache_read_input_tokens': read,
            'cache_creation_input_tokens': write,
            '_dispatch': {'provider_id': 'sankuai_anthropic'},
        },
        # Deliberately wrong, mirroring the pre-fix stamp: the cache-inclusive
        # total billed as if it were all uncached.
        'cost': {'costCny': 999.0, 'costUsd': 138.0,
                 'inputTokens': total, 'totalInputTokens': total + read + write},
    }


@pytest.fixture()
def live_conv():
    """Create a real conversation row carrying hybrid rounds; drop it after.

    Uses the production store + DB rather than a fake, because the defect being
    guarded WAS "the production write API does not exist". A double would have
    happily accepted the broken import.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg

    cid = 'zz_bftest_' + uuid.uuid4().hex[:10]
    messages = [
        {'role': 'user', 'content': 'hi'},
        {
            'role': 'assistant',
            'content': 'answer',
            'model': 'claude-opus-5',
            'provider_id': 'sankuai_anthropic',
            'cost': {'costCny': 999.0},
            'usage': {
                'input_tokens': 4, 'output_tokens': 200,
                'completion_tokens': 200,
                # Turn-level aggregate DRIFTS from the round sum on purpose:
                # measured fleet-wide on 115/117 turns. The migration must
                # derive the turn total from the rounds, not price this.
                'prompt_tokens': 200000 + 5000,
                'cache_read_input_tokens': 120000,
                'cache_creation_input_tokens': 60000,
            },
            'apiRounds': [
                _hybrid_round(2, 0, 100000, 'R1'),
                _hybrid_round(2, 100000, 20000, 'R2'),
            ],
        },
    ]

    db = get_thread_db(DOMAIN_CHAT)
    db.execute(
        'INSERT INTO conversations (id, user_id, title, messages, created_at, '
        'updated_at) VALUES (?, 1, ?, ?, ?, ?)',
        (cid, 'backfill test', json_dumps_pg(messages), 0, 0))
    db.commit()
    try:
        yield cid
    finally:
        try:
            db.execute('DELETE FROM conversations WHERE id=?', (cid,))
            db.commit()
        except Exception:
            pass


def _read(cid):
    from lib.agent_core.store import get_conversation_store
    loaded = get_conversation_store().load_conversation_messages(cid)
    assert loaded is not None, 'fixture conversation disappeared'
    return loaded


def test_apply_actually_writes_the_corrected_cost(live_conv):
    """THE regression: the writer must exist, run, and change cost ON DISK.

    Pre-fix this raised ImportError before writing anything.
    """
    mig = _load_migration()
    before, _, _ = _read(live_conv)
    assert before[1]['apiRounds'][0]['cost']['costCny'] == 999.0

    written, skipped, failed = mig.apply_plan([live_conv])
    assert (written, failed) == (1, 0), (
        'apply wrote=%d skipped=%d failed=%d' % (written, skipped, failed))

    after, _, _ = _read(live_conv)
    a = after[1]
    for ar in a['apiRounds']:
        assert ar['cost']['costCny'] != 999.0, 'round cost was not rewritten'
        # The uncached residual must now be the Anthropic key, not the total.
        assert ar['cost']['inputTokens'] == ar['usage']['input_tokens'], (
            'round still prices the cache-inclusive total as uncached input')
    assert a['cost']['costCny'] != 999.0, 'turn cost was not rewritten'


def test_apply_never_touches_the_raw_usage(live_conv):
    """``usage`` is the evidence the correction derives FROM — byte-identical.

    Rewriting it would destroy the ability to re-derive or audit the migration,
    and would also erase the turn-level aggregate drift that is itself signal.
    """
    mig = _load_migration()
    before, _, _ = _read(live_conv)
    usage_before = copy.deepcopy(
        [before[1]['usage']] + [r['usage'] for r in before[1]['apiRounds']])

    mig.apply_plan([live_conv])

    after, _, _ = _read(live_conv)
    usage_after = [after[1]['usage']] + [r['usage'] for r in after[1]['apiRounds']]
    assert json.dumps(usage_after, sort_keys=True) == \
        json.dumps(usage_before, sort_keys=True), \
        'usage was mutated — the migration destroyed its own evidence'


def test_apply_is_idempotent(live_conv):
    """A second run must be a no-op, not a second rewrite.

    Guards the re-run-after-partial-failure path the script advertises.
    """
    mig = _load_migration()
    w1, _, f1 = mig.apply_plan([live_conv])
    assert (w1, f1) == (1, 0)
    snap, _, rev1 = _read(live_conv)

    w2, s2, f2 = mig.apply_plan([live_conv])
    assert (w2, f2) == (0, 0), 'second run rewrote rows (not idempotent)'
    assert s2 == 1, 'second run should report the conversation as a no-op'

    snap2, _, rev2 = _read(live_conv)
    assert rev2 == rev1, 'a no-op run still bumped rev — it wrote anyway'
    assert json.dumps(snap2, sort_keys=True) == json.dumps(snap, sort_keys=True)


def test_a_lost_cas_race_is_replayed_not_clobbered(live_conv, monkeypatch):
    """A concurrent append must SURVIVE, and the correction must still land.

    The writer re-reads and replays on the fresh transcript. Re-pushing the
    copy it scanned would erase the sibling's row — the exact incident
    ``expected_rev`` exists to prevent (conv ms3sfyrmn31omb: 13 appends logged,
    8 survived).
    """
    from lib.agent_core.store import get_conversation_store
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg

    mig = _load_migration()
    store = get_conversation_store()
    real_load = store.load_conversation_messages
    state = {'n': 0}

    def _load_then_sibling_appends(cid):
        loaded = real_load(cid)
        if state['n'] == 0:
            # Simulate a sibling session appending AFTER our read but BEFORE
            # our write — this invalidates the rev we are about to CAS with.
            state['n'] += 1
            msgs, _u, _r = real_load(cid)
            msgs.append({'role': 'user', 'content': 'SIBLING APPEND'})
            db = get_thread_db(DOMAIN_CHAT)
            db.execute(
                'UPDATE conversations SET messages=?, updated_at=? WHERE id=?',
                (json_dumps_pg(msgs), 1, cid))
            db.commit()
        return loaded

    monkeypatch.setattr(store, 'load_conversation_messages',
                        _load_then_sibling_appends)
    written, _skipped, failed = mig.apply_plan([live_conv])
    monkeypatch.undo()

    assert failed == 0, 'the CAS conflict was not retried'
    assert written == 1, 'the correction never landed after the race'

    after, _, _ = _read(live_conv)
    texts = [m.get('content') for m in after if m.get('role') == 'user']
    assert 'SIBLING APPEND' in texts, (
        "the sibling's append was clobbered — the writer re-pushed its stale copy")
    a = [m for m in after if m.get('role') == 'assistant'][0]
    assert a['apiRounds'][0]['cost']['costCny'] != 999.0, (
        'the correction was lost while resolving the race')


def test_the_writer_uses_a_real_store_api():
    """WIRING: the symbols the apply path imports must actually resolve.

    This is the guard for the original defect. It is deliberately an IMPORT
    check rather than a source grep: a comment naming the right module cannot
    satisfy it, and a rename in the store package makes it red immediately
    instead of at the next real migration.
    """
    from lib.agent_core.store import get_conversation_store
    from lib.tasks_pkg.persistence_store import ConcurrentWriteConflict

    store = get_conversation_store()
    assert callable(getattr(store, 'load_conversation_messages', None)), \
        'store lost load_conversation_messages'
    assert callable(getattr(store, 'save_conversation_messages', None)), \
        'store lost save_conversation_messages'
    assert issubclass(ConcurrentWriteConflict, Exception)

    # ...and the migration module itself must import cleanly (the dead-import
    # defect was invisible until the writer ran).
    mig = _load_migration()
    assert callable(mig.apply_plan)


def test_scan_and_apply_share_one_correction():
    """The dry run must report exactly what the apply writes.

    Both call ``_correct_messages``; a second hand-written copy inside the
    writer is how a migration starts reporting one number and committing
    another.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parent / '_migrate_hybrid_cost_backfill.py'
    tree = ast.parse(src.read_text())
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef)}
    for caller in ('scan', 'apply_plan'):
        assert caller in fns, 'migration lost %s()' % caller
        called = {c.func.id for c in ast.walk(fns[caller])
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert '_correct_messages' in called, (
            '%s() does not call the shared correction — scan and apply can '
            'diverge' % caller)
