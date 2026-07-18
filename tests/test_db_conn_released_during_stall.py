"""A stalled ``run_task`` must NOT pin a DB connection slot for the whole stall.

Root cause closed here (finding-1 of the gateway-outage investigation): a
``run_task`` worker holds one ``_conn_semaphore`` slot from its first DB op
until ``close_thread_db()`` runs. That release used to live ONLY in the
terminal ``finally`` at ``_run.py`` — unreachable while the LLM dispatch loop
spins (e.g. a total gateway-5xx outage rotating slots). The stuck task then
pinned a connection slot for the WHOLE outage, and that semaphore is shared
with the frontend's data endpoints (``/api/v1/conversations``,
``/api/health``'s ``SELECT 1``), which could no longer acquire → "backend alive
but frontend can't be served".

The fix adds a PER-ROUND checkpoint release right before the LLM call
(``_run.py``, just before ``_llm_call_with_fallback``): the connection is
provably DB-idle there (per-round writes committed, streaming-tool pool does no
DB), so releasing it caps connection-hold at one round. The next DB op
transparently re-acquires via ``get_thread_db``.

This suite models that release at the store-seam level (the exact call the
orchestrator makes: ``get_conversation_store().release_connection()`` →
``close_thread_db()``) and proves:

  1. A worker that acquires a connection, RELEASES mid-work, then STALLS holds
     NO registry entry / semaphore slot during the stall — so a concurrent
     "frontend" query acquires immediately even while the worker is parked.
  2. NEUTER: a worker that stalls WITHOUT the mid-loop release keeps its
     registry entry for the whole stall (the pre-fix behaviour) — proving the
     release is what carries the property.
  3. Re-acquire after release works (get_thread_db transparently reconnects),
     and a final close returns to baseline (no leak from the checkpoint dance).

Run:  pytest tests/test_db_conn_released_during_stall.py -v
"""
from __future__ import annotations

import threading
import time

import pytest

import lib.database._core as core
from lib.database import DOMAIN_CHAT, close_thread_db, get_thread_db


def _registry_len() -> int:
    with core._thread_conn_lock:
        return len(core._thread_conn_registry)


def _my_registry_entries(thread) -> int:
    with core._thread_conn_lock:
        return sum(1 for (r, _c, _d) in core._thread_conn_registry if r() is thread)


def _release_via_store():
    """The EXACT seam run_task uses for the per-round checkpoint release."""
    from lib.agent_core.store import get_conversation_store
    get_conversation_store().release_connection()


@pytest.mark.unit
class TestConnReleasedDuringStall:

    def test_midloop_release_frees_slot_during_stall(self, flask_client):
        """A worker that releases mid-loop then stalls must hold no connection
        entry during the stall, so a concurrent query can acquire."""
        if core._BACKEND != 'pg':
            pytest.skip('registry/semaphore accounting is PG-only')

        core._reap_dead_thread_connections()
        baseline = _registry_len()

        stall_release = threading.Event()   # test → worker: you may finish
        worker_parked = threading.Event()   # worker → test: I've released + parked
        worker_thread_box = {}

        def worker():
            worker_thread_box['t'] = threading.current_thread()
            # Round work: acquire + use a connection (pins a slot).
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('SELECT 1')
            db.commit()
            assert _my_registry_entries(threading.current_thread()) == 1

            # ★ Per-round checkpoint release (the fix), THEN stall — models the
            #   spinning dispatch loop that never reaches the terminal finally.
            _release_via_store()
            worker_parked.set()
            stall_release.wait(timeout=10)  # "spin" until the test releases us

        wt = threading.Thread(target=worker, name='stall-worker')
        wt.start()
        assert worker_parked.wait(timeout=5), 'worker never parked'

        # While the worker is STALLED, it must hold NO connection entry.
        me = worker_thread_box['t']
        assert _my_registry_entries(me) == 0, (
            'stalled worker still pinned a connection after mid-loop release')

        # And a concurrent "frontend" query acquires + releases fine meanwhile.
        fe_ok = {}
        def frontend_query():
            d = get_thread_db(DOMAIN_CHAT)
            row = d.execute('SELECT 1').fetchone()
            fe_ok['v'] = (row[0] if row is not None else None)
            close_thread_db()
        fq = threading.Thread(target=frontend_query, name='frontend')
        fq.start()
        fq.join(timeout=5)
        assert fe_ok.get('v') == 1, 'concurrent frontend query could not run during stall'

        stall_release.set()
        wt.join(timeout=5)

        core._reap_dead_thread_connections()
        assert _registry_len() == baseline, 'registry did not return to baseline'

    def test_neuter_no_release_pins_slot_during_stall(self, flask_client):
        """NEUTER: without the mid-loop release, the stalled worker keeps its
        connection entry for the whole stall — the pre-fix behaviour that the
        checkpoint release exists to eliminate."""
        if core._BACKEND != 'pg':
            pytest.skip('registry accounting is PG-only')

        stall_release = threading.Event()
        worker_parked = threading.Event()
        box = {}

        def worker_no_release():
            box['t'] = threading.current_thread()
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('SELECT 1')
            db.commit()
            # NO _release_via_store() here — this is the neutered path.
            worker_parked.set()
            stall_release.wait(timeout=10)
            close_thread_db()  # only at the very end (like the terminal finally)

        wt = threading.Thread(target=worker_no_release, name='neuter-worker')
        wt.start()
        assert worker_parked.wait(timeout=5)
        try:
            # Pre-fix: the entry is STILL held during the stall.
            assert _my_registry_entries(box['t']) == 1, (
                'expected the neutered (no mid-loop release) worker to still '
                'pin its connection during the stall')
        finally:
            stall_release.set()
            wt.join(timeout=5)

    def test_reacquire_after_release_is_transparent(self, flask_client):
        """After a mid-loop release the next DB op transparently re-acquires,
        and a final close returns to baseline (checkpoint dance is leak-free)."""
        if core._BACKEND != 'pg':
            pytest.skip('registry accounting is PG-only')

        core._reap_dead_thread_connections()
        baseline = _registry_len()
        result = {}

        def worker():
            me = threading.current_thread()
            get_thread_db(DOMAIN_CHAT).execute('SELECT 1')
            _release_via_store()
            after_release = _my_registry_entries(me)
            # Next round's first DB op re-acquires transparently.
            row = get_thread_db(DOMAIN_CHAT).execute('SELECT 1').fetchone()
            after_reacquire = _my_registry_entries(me)
            close_thread_db()
            result.update(after_release=after_release,
                          after_reacquire=after_reacquire,
                          value=(row[0] if row is not None else None))

        wt = threading.Thread(target=worker, name='reacquire-worker')
        wt.start()
        wt.join(timeout=5)

        assert result['after_release'] == 0, 'release did not drop the entry'
        assert result['after_reacquire'] == 1, 're-acquire did not re-register'
        assert result['value'] == 1
        core._reap_dead_thread_connections()
        assert _registry_len() == baseline


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
