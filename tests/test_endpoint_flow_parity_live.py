"""tests/test_endpoint_flow_parity_live.py — REAL-MODEL parity (manual only).

The final validation gate before flipping ``TOFU_ENDPOINT_VIA_FLOW`` on by
default. Unlike ``test_endpoint_flow_parity.py`` (scripted LLM, runs in CI),
this hits the **real LLM** on a non-trivial coding task through BOTH paths
and diffs the persisted conversation rows.

It is DOUBLE-GATED so it never runs in CI or a normal ``pytest`` invocation:
  1. ``@pytest.mark.live_llm`` marker (registered in pyproject.toml).
  2. ``skipif`` unless ``TOFU_PARITY_LIVE=1``.

It also needs working LLM creds (``LLM_API_KEYS`` / ``LLM_BASE_URL`` /
``LLM_MODEL``, or the Settings-configured equivalent) — if dispatch can't
resolve a model the test skips with a clear message rather than failing.

Run it manually::

    TOFU_PARITY_LIVE=1 LLM_API_KEYS=sk-... LLM_MODEL=gpt-4o \\
        python -m pytest tests/test_endpoint_flow_parity_live.py -s -m live_llm

Because the real model is non-deterministic, we DO NOT compare content
byte-for-byte. We compare the STRUCTURE that defines a drop-in:
  * turn skeleton — ordered (role, kind) where kind ∈ planner/worker/critic,
  * every turn has non-empty content,
  * the loop reached a critic verdict (an _isEndpointReview turn exists),
  * both paths agree on the SET of turn kinds present.

A human reading the ``-s`` output also gets both transcripts side-by-side
for a qualitative gut-check the assertions can't make.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.log import get_logger

logger = get_logger(__name__)

_LIVE = os.environ.get('TOFU_PARITY_LIVE') == '1'
_SKIP_REASON = ('real-model parity is manual-only — set TOFU_PARITY_LIVE=1 '
                '(and LLM creds) to run')

# A small but genuinely non-trivial coding task: needs a plan, a real edit,
# and a verifiable acceptance check (so the critic has something to judge).
_TASK_TEXT = (
    'Create a new file scratch_parity_demo.py containing a function '
    'is_palindrome(s: str) -> bool that ignores case and non-alphanumeric '
    'characters, plus three assert-based self-tests at the bottom under '
    'a __main__ guard. Then run it to confirm the asserts pass.'
)


# ── conversation-row helpers (shared shape with the scripted test) ──

# Seeded rows hit the SAME production DB the app serves from (get_thread_db has
# no test-DB isolation), so each one MUST be deleted afterwards or it leaks into
# the user's sidebar. Track + purge via the autouse cleanup fixture below.
_CREATED_CONVS: set[str] = set()


def _seed_conversation(conv_id: str, user_text: str):
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database._core_schema import CONVERSATIONS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    messages = [{'role': 'user', 'content': user_text}]
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'parity-live',
        'messages': json.dumps(messages), 'created_at': now, 'updated_at': now,
        'settings': '{}', 'msg_count': len(messages),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                    'updated_at', 'settings', 'msg_count'], commit=True)
    _CREATED_CONVS.add(conv_id)


def _delete_conversations(conv_ids):
    """Remove seeded conversation rows from the production DB."""
    if not conv_ids:
        return
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    for cid in conv_ids:
        try:
            db.execute('DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
        except Exception as e:
            logger.warning('live parity cleanup failed for conv=%s: %s', cid, e)
    db.commit()


@pytest.fixture(autouse=True)
def _cleanup_seeded_convs():
    """Delete every conversation row seeded during the test from the prod DB."""
    _CREATED_CONVS.clear()
    try:
        yield
    finally:
        _delete_conversations(set(_CREATED_CONVS))
        _CREATED_CONVS.clear()


def _read_turns(conv_id: str):
    """Return the endpoint turns as [(role, kind, content)]."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT messages FROM conversations WHERE id=? AND user_id=1', (conv_id,)
    ).fetchone()
    if not row:
        return []
    msgs = json.loads(row[0] or '[]')
    turns = []
    for m in msgs:
        role = m.get('role')
        content = m.get('content') or ''
        if m.get('_isEndpointPlanner'):
            turns.append((role, 'planner', content))
        elif m.get('_isEndpointReview'):
            turns.append((role, 'critic', content))
        elif m.get('_epIteration'):
            turns.append((role, 'worker', content))
    return turns


def _fingerprint(turns):
    """Structural fingerprint for parity comparison (content-agnostic)."""
    return {
        'skeleton': [(r, k) for r, k, _c in turns],
        'kind_set': sorted(set(k for _r, k, _c in turns)),
        'has_critic': any(k == 'critic' for _r, k, _c in turns),
        'all_nonempty': all(bool(c.strip()) for _r, _k, c in turns),
        'n_turns': len(turns),
    }


def _make_task(conv_id: str, user_text: str):
    from lib.tasks_pkg.manager import create_task
    config = {
        'endpointMode': True,
        'endpointMaxIterations': 3,
        'projectEnabled': True,        # needs write_file + run_command
        'codeExecEnabled': True,
        'searchMode': 'off',
        'browserEnabled': False,
    }
    messages = [{'role': 'user', 'content': user_text}]
    return create_task(conv_id, messages, config)


def _wait_done(fn, task, timeout=600):
    done = threading.Event()
    err = []

    def _w():
        try:
            fn(task)
        except Exception as e:
            logger.error('live parity task failed: %s', e, exc_info=True)
            err.append(e)
        finally:
            done.set()

    threading.Thread(target=_w, daemon=True).start()
    if not done.wait(timeout=timeout):
        task['aborted'] = True
        raise TimeoutError('live endpoint task did not finish within %ss' % timeout)
    if err:
        raise err[0]


def _dispatch_available() -> bool:
    """True iff the real dispatcher can resolve at least one model/key."""
    try:
        from lib.llm_dispatch import dispatch_chat  # noqa: F401
        keys = (os.environ.get('LLM_API_KEYS') or '').strip()
        # Settings-configured keys also work; env is the simplest probe.
        return bool(keys) or bool(os.environ.get('LLM_BASE_URL'))
    except Exception:
        return False


@pytest.mark.live_llm
@pytest.mark.skipif(not _LIVE, reason=_SKIP_REASON)
class TestEndpointFlowParityLive:
    def test_both_paths_real_model_parity(self, capsys):
        if not _dispatch_available():
            pytest.skip('no LLM creds resolvable (set LLM_API_KEYS / LLM_BASE_URL)')

        from lib.tasks_pkg.endpoint import run_endpoint_task
        from lib.orchestration_endpoint_runner import run_endpoint_via_flow

        # ── Live path ──
        os.environ.pop('TOFU_ENDPOINT_VIA_FLOW', None)
        conv_live = f'parity-live-real-{uuid.uuid4().hex[:8]}'
        _seed_conversation(conv_live, _TASK_TEXT)
        t0 = time.monotonic()
        _wait_done(run_endpoint_task, _make_task(conv_live, _TASK_TEXT))
        live_turns = _read_turns(conv_live)
        live_dt = time.monotonic() - t0

        # ── Flagged FlowExecutor path ──
        os.environ['TOFU_ENDPOINT_VIA_FLOW'] = '1'
        try:
            conv_flow = f'parity-flow-real-{uuid.uuid4().hex[:8]}'
            _seed_conversation(conv_flow, _TASK_TEXT)
            t1 = time.monotonic()
            _wait_done(run_endpoint_via_flow, _make_task(conv_flow, _TASK_TEXT))
            flow_turns = _read_turns(conv_flow)
            flow_dt = time.monotonic() - t1
        finally:
            os.environ.pop('TOFU_ENDPOINT_VIA_FLOW', None)

        live_fp = _fingerprint(live_turns)
        flow_fp = _fingerprint(flow_turns)

        # ── Human-readable side-by-side dump (visible with -s) ──
        with capsys.disabled():
            print('\n' + '=' * 70)
            print('REAL-MODEL ENDPOINT PARITY')
            print('=' * 70)
            for label, turns, dt, fp in (
                ('LIVE  ', live_turns, live_dt, live_fp),
                ('FLOW  ', flow_turns, flow_dt, flow_fp),
            ):
                print(f'\n── {label} ({dt:.1f}s, {fp["n_turns"]} turns, '
                      f'kinds={fp["kind_set"]}) ──')
                for r, k, c in turns:
                    preview = c.strip().replace('\n', ' ')[:100]
                    print(f'  [{k:7}|{r:9}] {preview}')
            print('\n── fingerprints ──')
            print('  live:', live_fp)
            print('  flow:', flow_fp)
            print('=' * 70)

        # ── Structural parity assertions (content-agnostic) ──
        assert live_turns, 'live path persisted no endpoint turns'
        assert flow_turns, 'flagged path persisted no endpoint turns'
        for label, fp in (('live', live_fp), ('flow', flow_fp)):
            assert fp['kind_set'][:1] != [] and 'planner' in fp['kind_set'], \
                f'{label} produced no planner turn: {fp}'
            assert 'worker' in fp['kind_set'], f'{label} produced no worker turn: {fp}'
            assert fp['has_critic'], f'{label} never reached a critic verdict: {fp}'
            assert fp['all_nonempty'], f'{label} has an empty turn: {fp}'
            # roles must line up: critic=user, planner/worker=assistant
            for r, k in fp['skeleton']:
                assert (r == 'user') == (k == 'critic'), \
                    f'{label} role/kind mismatch: ({r},{k})'

        # The decisive parity check: same SET of turn kinds present.
        assert live_fp['kind_set'] == flow_fp['kind_set'], (
            f'turn-kind sets differ — NOT a drop-in.\n'
            f'  live={live_fp["kind_set"]}\n  flow={flow_fp["kind_set"]}')


if __name__ == '__main__':
    import unittest
    unittest.main()
