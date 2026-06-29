"""tests/test_timer_parse_failure.py — Timer Watcher parse-failure diagnostics.

Covers the 2026-06-23 change that makes an unparseable poll decision
locatable and inspectable instead of a bare truncated reason:
  * ``poll_timer`` returns a 9-tuple whose last element is the LLM's FULL
    raw output, and flags ``parse_error=True`` when the decision is not JSON.
  * ``_record_poll`` persists the new ``poll_id`` + ``raw_output`` columns.
  * A clean (parseable) decision still returns ``parse_error=False`` and an
    empty/ignored raw dump path.

Uses the session SQLite DB from conftest (TOFU_DB_PATH) — no PG needed.
"""

import json

import pytest

import lib.scheduler.timer as timer_mod
from lib.database import DOMAIN_SYSTEM, get_thread_db

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema():
    """Ensure the v27 timer_poll_log columns (poll_id, raw_output) exist.

    Production adds these via init_db()'s migration on the next server
    restart. Running the full init_db() here would contend with a live
    server's locks, so we apply just the two idempotent ALTERs the v27
    migration performs (IF NOT EXISTS where the backend supports it).
    """
    db = get_thread_db(DOMAIN_SYSTEM)
    for col in ('poll_id', 'raw_output'):
        try:
            db.execute(f"ALTER TABLE timer_poll_log ADD COLUMN IF NOT EXISTS {col} "
                       f"TEXT NOT NULL DEFAULT ''")
            db.commit()
        except Exception:
            # SQLite lacks IF NOT EXISTS on ADD COLUMN; fall back to a probe.
            try:
                db.execute(f'SELECT {col} FROM timer_poll_log LIMIT 1').fetchall()
            except Exception:
                db.execute(f"ALTER TABLE timer_poll_log ADD COLUMN {col} "
                           f"TEXT NOT NULL DEFAULT ''")
                db.commit()


@pytest.fixture(autouse=True)
def _cleanup_created_timers():
    """Delete every timer this test module creates, so no ``active`` row leaks
    into the (possibly shared/production) DB and gets resurrected by
    ``resume_active_timers()`` on the next server restart — the 2026-06-26
    zombie-timer search-storm root cause. Pattern-gated to this module's
    synthetic conv id, so it can never touch a real user's timer.
    """
    yield
    try:
        db = get_thread_db(DOMAIN_SYSTEM)
        db.execute("DELETE FROM timer_watchers WHERE conv_id='conv-parsefail'")
        db.commit()
    except Exception:
        pass


def _make_timer():
    """Create an active timer (no check_command → always calls the LLM)."""
    t = timer_mod.create_timer(
        conv_id='conv-parsefail',
        check_instruction='Is the run finished?',
        continuation_message='Summarize the results.',
        poll_interval=10,
        max_polls=120,
        check_command='',
        tools_config={},
        source_task_id='task-x',
    )
    return t['id']


def test_poll_timer_parse_failure_returns_raw(monkeypatch):
    """A non-JSON LLM reply → parse_error + the full raw text in slot 9."""
    timer_id = _make_timer()
    raw = 'From the JSON file, there are **29 genomes** and history length 29.'

    def _fake_smart_chat(messages, **kwargs):
        # No tool calls; content is prose, not JSON → parse must fail.
        return raw, {'total_tokens': 42, '_dispatch': {'model': 'deepseek-v4-flash-tencent'}}

    import lib.llm_dispatch as _ld
    monkeypatch.setattr(_ld, 'smart_chat', _fake_smart_chat, raising=True)

    result = timer_mod.poll_timer(timer_id)
    assert len(result) == 9, 'poll_timer must return a 9-tuple (raw_content added)'
    ready, reason, tokens, skipped, parse_error, cmd_output, model, trace, raw_content = result

    assert ready is False
    assert skipped is False
    assert parse_error is True
    assert tokens == 42
    assert model == 'deepseek-v4-flash-tencent'
    assert raw_content == raw, 'raw_content must be the LLM output verbatim (untruncated)'
    assert 'See raw output below' in reason


def test_poll_timer_clean_decision_no_parse_error(monkeypatch):
    """A valid JSON decision → parse_error False and the decision honored."""
    timer_id = _make_timer()

    def _fake_smart_chat(messages, **kwargs):
        return json.dumps({'ready': True, 'reason': 'done'}), {'total_tokens': 7}

    import lib.llm_dispatch as _ld
    monkeypatch.setattr(_ld, 'smart_chat', _fake_smart_chat, raising=True)

    ready, reason, tokens, skipped, parse_error, cmd_output, model, trace, raw_content = \
        timer_mod.poll_timer(timer_id)
    assert ready is True
    assert parse_error is False
    assert reason == 'done'


def test_record_poll_persists_id_and_raw():
    """_record_poll writes poll_id + raw_output; get_timer_poll_log reads them back."""
    timer_id = _make_timer()
    poll_id = f'{timer_id}.p1'
    raw = 'unparseable model output here'

    timer_mod._record_poll(
        timer_id, 'parse_error', 'Could not parse the verification decision', 13,
        check_output='', model='deepseek-v4-flash-tencent',
        poll_id=poll_id, raw_output=raw,
    )

    log = timer_mod.get_timer_poll_log(timer_id, limit=5)
    assert log, 'poll log must have at least one row'
    row = log[0]
    assert row['poll_id'] == poll_id
    assert row['raw_output'] == raw
    assert row['decision'] == 'parse_error'


def test_record_poll_clean_omits_raw():
    """A clean wait/ready poll stores an empty raw_output (no needless dump)."""
    timer_id = _make_timer()
    timer_mod._record_poll(
        timer_id, 'wait', 'still running', 5,
        poll_id=f'{timer_id}.p1', raw_output='',
    )
    row = timer_mod.get_timer_poll_log(timer_id, limit=1)[0]
    assert row['raw_output'] == ''
    assert row['poll_id'] == f'{timer_id}.p1'


def test_timer_poll_log_has_new_columns():
    """The live timer_poll_log table carries poll_id + raw_output columns.

    Backend-agnostic: a SELECT of the two columns succeeds only if they exist.
    """
    db = get_thread_db(DOMAIN_SYSTEM)
    db.execute('SELECT poll_id, raw_output FROM timer_poll_log LIMIT 1').fetchall()
