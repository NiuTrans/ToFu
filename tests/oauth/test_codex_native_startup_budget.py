"""Learned cold native startup budget; synthetic Codex only, never the host login.

A healthy but slow app-server start (e.g. a Codex home on network storage) must
converge instead of failing forever at the warm exchange budget, while a hung
native command still stops at the ceiling and warm session reuse keeps its
own bound.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import time

import pytest

from lib.oauth import codex_command_login as command
from lib.oauth.codex_local_login import read_local_login
from tests.oauth.test_codex_command_login import native as native, _events, _expire_metadata, _token

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name == 'nt', reason='POSIX native-shell adapter')]

# Captured at import, before the shared fixture shortens budgets for speed.
PRODUCTION_EXCHANGE_SECONDS = command._EXCHANGE_SECONDS


def _context(native, environment=None, **kwargs):
    return command.command_context(environment=environment or native['env'], **kwargs)


def _revision(native, environment=None):
    return command._environment_revision(environment or native['env'], native['home'])


def _startup_budget(native, environment=None):
    return command._startup_seconds(_revision(native, environment))


def _slow_initialize(native, monkeypatch, *, default=1.0, seconds=1.05):
    """Default budget < synthetic spawn+initialize < twice the default."""
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', default)
    native['mode'].write_text(f'slow-initialize:{seconds}')


def _await_parked_reaper():
    """Let the idle reaper a new session started finish its first lock hold.

    Revalidation admission is a non-blocking acquire by design: a read that
    meets the reaper's brief hold serves verified metadata and defers the
    revalidation to a later read. Tests of admission must not race that hold.
    """
    deadline = time.monotonic() + 10
    while command._session_wake.is_set() and time.monotonic() < deadline:
        time.sleep(0.001)
    with command._lock:
        pass


def test_production_budgets_keep_four_second_exchange_and_thirty_second_ceiling(monkeypatch):
    monkeypatch.delenv('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS', raising=False)
    assert PRODUCTION_EXCHANGE_SECONDS == 4.0
    assert command._startup_ceiling() == 30.0
    assert command._STARTUP_BUDGET_SIGNATURES == 16
    assert command._STARTUP_EXHAUSTED_SECONDS == 60.0


def test_slow_cold_start_succeeds_after_one_doubling_and_then_immediately(native, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=command.__name__)
    _slow_initialize(native, monkeypatch)

    before = time.monotonic()
    assert _context(native) == {'shared_login_issue': 'codex_command_timeout'}
    assert time.monotonic() - before < 2.5
    assert _startup_budget(native) == 2.0
    assert 'reason=initialize_timeout' in caplog.text and 'budget_ms=2000' in caplog.text

    before = time.monotonic()
    recovered = _context(native, retry_failed=True)
    elapsed = time.monotonic() - before
    assert recovered['codex_home'] == str(native['linked']) and not recovered['shared_login_issue']
    learned = _startup_budget(native)
    assert 2 * 1.05 <= learned <= 2 * elapsed + 0.01
    assert 'reason=initialize_observed' in caplog.text
    assert str(native['home']) not in caplog.text and _revision(native) not in caplog.text

    # Rotation, reconnect or renewal starts a new app-server: it fits at once.
    command.clear_command_context()
    caplog.clear()
    assert not _context(native)['shared_login_issue']
    assert 'codex_command_timeout' not in caplog.text
    assert [event['event'] for event in _events(native)].count('initialize') == 3
    assert all(child.poll() is not None for child in native['processes'][:-1])


def test_renewal_and_its_lock_wait_use_the_learned_cold_budget(native, monkeypatch):
    _slow_initialize(native, monkeypatch)
    command._learn_startup_seconds(_revision(native), 1.5, reason='synthetic')
    assert not _context(native)['shared_login_issue']
    native['auth'].write_text(json.dumps(_token(expired=True)))

    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(pool.map(
            lambda _: read_local_login(environment=native['env'], refresh=True), range(2)))

    # The one-shot renewal app-server starts slower than the 1 s default; the
    # concurrent caller waits behind that cold flight, then reads its result.
    assert all(snapshot.get('access_token') for snapshot in snapshots), snapshots
    assert [event['event'] for event in _events(native)].count('account/read') == 1
    assert json.loads(native['auth'].read_text())['tokens']['refresh_token'] == 'never-copy-refresh'
    session, renewal = native['processes']
    assert session.poll() is None and command._session is not None
    assert renewal.poll() is not None and renewal.stdin.closed and renewal.stdout.closed


def test_blocking_discovery_waits_behind_a_cold_start_instead_of_timing_out(native, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=command.__name__)
    _slow_initialize(native, monkeypatch)
    command._learn_startup_seconds(_revision(native), 1.5, reason='synthetic')

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_context, native)
        started = time.monotonic()
        while not command._lock.locked() and time.monotonic() - started < 5:
            time.sleep(0.005)
        assert command._lock.locked()
        # Queued behind a cold start that outlasts one exchange budget.
        second = _context(native)
        assert not first.result(timeout=10)['shared_login_issue']

    assert second['codex_home'] == str(native['linked']) and not second['shared_login_issue']
    assert 'phase=lock' not in caplog.text
    events = [event['event'] for event in _events(native)]
    assert events.count('start') == events.count('initialize') == 1


def test_unavailable_explicit_home_keeps_the_exchange_wait_despite_a_learned_budget(native, monkeypatch):
    environment = {**native['env'], 'CODEX_HOME': str(native['home'] / 'operator-home-not-restored')}
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', 0.2)
    command._learn_startup_seconds(_revision(native, environment), 5.0, reason='synthetic')
    assert _startup_budget(native, environment) == 10.0

    assert command._lock.acquire(timeout=1)
    try:
        before = time.monotonic()
        assert _context(native, environment) == {'shared_login_issue': 'codex_home_unavailable'}
        assert time.monotonic() - before < 1.0
    finally:
        command._lock.release()
    assert native['processes'] == []


def test_demand_revalidation_rotates_with_the_learned_cold_budget(native, monkeypatch):
    _slow_initialize(native, monkeypatch)
    command._learn_startup_seconds(_revision(native), 1.5, reason='synthetic')
    assert not _context(native)['shared_login_issue']
    first = command._session
    first['born'] -= command._SESSION_ROTATE_SECONDS
    _expire_metadata(monkeypatch)
    _await_parked_reaper()

    before = time.monotonic()
    assert not _context(native)['shared_login_issue']  # Verified metadata; no wait.
    assert time.monotonic() - before < 1.0
    worker = command._metadata_probe
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert command._session is not None and command._session is not first
    assert command._cached[2]['codex_home'] == str(native['linked'])
    assert [event['event'] for event in _events(native)].count('initialize') == 2
    assert native['processes'][0].poll() is not None


def test_hung_native_startup_fails_at_the_ceiling_then_refuses_cold_starts(native, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=command.__name__)
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', 0.2)
    monkeypatch.setenv('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS', '0.7')
    native['mode'].write_text('hang')
    timeout = {'shared_login_issue': 'codex_command_timeout'}
    remaining = []
    for _ in range(3):
        before = time.monotonic()
        assert _context(native, retry_failed=True) == timeout
        elapsed = time.monotonic() - before
        remaining.append(command._cached[1] - time.monotonic())
    assert _startup_budget(native) == 0.7  # 0.2 -> 0.4 -> 0.7 (clamped)
    assert 0.7 <= elapsed < 0.7 + 1.5
    # Below the ceiling a timeout keeps the 5 s failure cache; at it, the window.
    assert max(remaining[:2]) <= 5.0 and 55.0 < remaining[2] <= 60.0
    assert 'ceiling_ms=700' in caplog.text and 'cold starts refused for 60 s' in caplog.text

    # Recovery and passive reads no longer re-pay the ceiling.
    before = time.monotonic()
    assert _context(native, retry_failed=True) == timeout
    assert _context(native) == timeout
    assert time.monotonic() - before < 0.5
    assert len(native['processes']) == 3

    # Clear (logout/reconnect) retries at once; the kept budget refills the window.
    command.clear_command_context()
    assert _context(native, retry_failed=True) == timeout
    assert len(native['processes']) == 4
    # A changed ceiling ends the window and resumes convergence.
    monkeypatch.setenv('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS', '1.4')
    assert _context(native, retry_failed=True) == timeout
    assert len(native['processes']) == 5 and _startup_budget(native) == 1.4
    assert command._cached[1] - time.monotonic() <= 5.0  # 0.7 s was below the new ceiling.
    assert all(child.poll() is not None and child.stdin.closed and child.stdout.closed
               for child in native['processes'])


def test_renewal_that_exhausts_the_ceiling_is_not_retried_serially(native, monkeypatch):
    assert not _context(native)['shared_login_issue']  # Warm metadata session.
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', 0.2)
    monkeypatch.setenv('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS', '0.6')
    command._learn_startup_seconds(_revision(native), 0.3, reason='synthetic')
    assert _startup_budget(native) == 0.6  # At the ceiling.
    native['auth'].write_text(json.dumps(_token(expired=True)))
    native['mode'].write_text('hang-initialize')  # Only new app-servers hang.
    expected = {'shared_login_issue': 'codex_command_timeout', 'account_id': 'account-a'}

    assert read_local_login(environment=native['env'], refresh=True) == expected  # Pays it once.
    before = time.monotonic()
    for _ in range(3):
        assert read_local_login(environment=native['env'], refresh=True) == expected
    assert time.monotonic() - before < 0.5
    assert len(native['processes']) == 2  # The metadata session and one renewal.
    assert command._session is not None and native['processes'][0].poll() is None


def test_a_start_cut_short_by_its_flight_never_opens_the_refusal_window(native, monkeypatch):
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', 0.2)
    monkeypatch.setenv('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS', '0.6')
    command._learn_startup_seconds(_revision(native), 0.3, reason='synthetic')  # At the ceiling.
    native['mode'].write_text('hang')
    assert command._lock.acquire(timeout=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_context, native)
        time.sleep(0.4)  # The lock wait spends half of the 0.8 s flight.
        command._lock.release()
        assert pending.result(timeout=5) == {'shared_login_issue': 'codex_command_timeout'}
    assert command._cached[1] - time.monotonic() <= 5.0
    assert _context(native, retry_failed=True) == {'shared_login_issue': 'codex_command_timeout'}
    assert len(native['processes']) == 2  # Recovery still started native Codex again.


def test_warm_session_reuse_keeps_the_exchange_budget(native, monkeypatch, caplog):
    assert not _context(native)['shared_login_issue']
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', 0.3)
    command._learn_startup_seconds(_revision(native), 3.0, reason='synthetic')
    assert _startup_budget(native) == 6.0
    native['mode'].write_text('hang-config/read')
    _expire_metadata(monkeypatch)
    caplog.set_level(logging.DEBUG, logger=command.__name__)

    before = time.monotonic()
    assert not _context(native, retry_failed=True)['shared_login_issue']  # Bounded grace.
    assert time.monotonic() - before < 0.3 + 1.5
    assert 'phase=session_reuse' in caplog.text
    assert 'phase=config issue=codex_command_timeout' in caplog.text and 'budget_ms=300' in caplog.text
    events = [event['event'] for event in _events(native)]
    assert events.count('start') == events.count('initialize') == 1
    assert all(child.poll() is not None for child in native['processes'])


def test_learned_startup_budget_is_isolated_per_launcher_environment(native, monkeypatch):
    _slow_initialize(native, monkeypatch)
    other = {**native['env'], 'SYNTHETIC_LAUNCHER_INPUT': 'other'}
    assert _context(native) == {'shared_login_issue': 'codex_command_timeout'}
    assert (_startup_budget(native), _startup_budget(native, other)) == (2.0, 1.0)
    # Another launcher environment starts from the default, not A's timing.
    assert _context(native, other) == {'shared_login_issue': 'codex_command_timeout'}
    assert not _context(native, retry_failed=True)['shared_login_issue']
    assert _startup_budget(native) > 2.0 and _startup_budget(native, other) == 2.0


def test_caller_truncated_startup_timeout_does_not_double_the_budget(native, monkeypatch):
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', 1.0)
    native['mode'].write_text('hang')
    with command._lock:
        with pytest.raises(command._CommandFailure, match='codex_command_timeout'):
            command._exchange(native['env'], native['home'], timeout_seconds=0.3)
    assert _startup_budget(native) == 1.0
    assert command._session is None
    assert all(child.poll() is not None for child in native['processes'])


def test_learned_budgets_are_bounded_clamped_and_kept_across_clear(monkeypatch):
    budgets = OrderedDict()
    monkeypatch.setattr(command, '_startup_budgets', budgets)
    monkeypatch.delenv('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS', raising=False)
    for index in range(command._STARTUP_BUDGET_SIGNATURES + 1):
        command._learn_startup_seconds(f'launcher-{index}', 2.5 + index, reason='synthetic')
    assert len(budgets) == command._STARTUP_BUDGET_SIGNATURES
    assert command._startup_seconds('launcher-0') == 4.0  # Least recent; evicted.
    assert command._startup_seconds('launcher-1') == 7.0
    assert command._startup_seconds('launcher-16') == 30.0  # Never above the ceiling.
    monkeypatch.setenv('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS', '10')
    assert command._startup_seconds('launcher-16') == 10.0
    command._learn_startup_seconds('launcher-16', 0.4, reason='synthetic')
    assert command._startup_seconds('launcher-16') == 7.5  # Decayed by a quarter, not reset.
    for _ in range(3):
        command._learn_startup_seconds('launcher-16', 0.4, reason='synthetic')
    assert 'launcher-16' not in budgets and command._startup_seconds('launcher-16') == 4.0
    command.clear_command_context()  # Timing hints are not source authority.
    assert command._startup_seconds('launcher-1') == 7.0


@pytest.mark.parametrize('raw,expected,rejected', [
    ('', 30.0, False), ('45', 45.0, False), (' 12.5 ', 12.5, False), ('4', 4.0, False),
    ('300', 300.0, False), ('3.9', 30.0, True), ('300.5', 30.0, True), ('0', 30.0, True),
    ('-5', 30.0, True), ('nan', 30.0, True), ('inf', 30.0, True), ('1e400', 30.0, True),
    ('30s', 30.0, True), ('abc', 30.0, True),
])
def test_startup_ceiling_override_is_validated(monkeypatch, caplog, raw, expected, rejected):
    monkeypatch.setattr(command, '_startup_ceiling_rejected', '')
    monkeypatch.setenv('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS', raw)
    caplog.set_level(logging.WARNING, logger=command.__name__)
    assert command._startup_ceiling() == expected
    assert command._startup_ceiling() == expected
    assert caplog.text.count('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS ignored') == (1 if rejected else 0)


def _wait_for(predicate, timeout=5.0):
    started = time.monotonic()
    while not predicate():
        assert time.monotonic() - started < timeout
        time.sleep(0.005)


def _initialize_count(native):
    return ([event['event'] for event in _events(native)].count('initialize')
            if native['trace'].exists() else 0)


@pytest.mark.parametrize('flight', ['discovery', 'renewal'])
def test_clear_cancels_an_in_flight_cold_start_within_child_teardown(native, caplog, flight):
    caplog.set_level(logging.INFO, logger=command.__name__)
    if flight == 'renewal':
        assert not _context(native)['shared_login_issue']  # Warm metadata session.
        native['auth'].write_text(json.dumps(_token(expired=True)))
        native['mode'].write_text('hang-initialize')  # Only the renewal starts native Codex.
        start = lambda: read_local_login(environment=native['env'], refresh=True)  # noqa: E731
        expected = {'shared_login_issue': 'codex_command_unavailable', 'account_id': 'account-a'}
    else:
        native['mode'].write_text('hang')
        start = lambda: _context(native)  # noqa: E731
        expected = {'shared_login_issue': 'codex_command_unavailable'}
    # A launcher that legitimately needed the whole ceiling: one flight may
    # hold the native-auth lock for 30 s plus an exchange.
    command._learn_startup_seconds(_revision(native), 30.0, reason='synthetic')
    assert _startup_budget(native) == 30.0
    initialized = _initialize_count(native)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(start)
        _wait_for(lambda: _initialize_count(native) == initialized + 1)
        before = time.monotonic()
        command.clear_command_context()
        cleared = time.monotonic() - before
        assert pending.result(timeout=5) == expected

    assert cleared < 3.0, cleared
    assert 'cancelled phase=initialize' in caplog.text
    assert str(native['home']) not in caplog.text and _revision(native) not in caplog.text
    assert _startup_budget(native) == 30.0  # A cancellation is no startup evidence.
    assert command._cached is None and command._verified is None and command._session is None
    assert all(child.poll() is not None and child.stdin.closed and child.stdout.closed
               for child in native['processes'])
    # Clear has returned: the next flight is not cancelled.
    native['mode'].write_text('')
    native['auth'].write_text(json.dumps(_token()))
    assert read_local_login(environment=native['env'])['access_token']


def test_discovery_and_renewal_share_one_native_flight(native, monkeypatch):
    assert not _context(native)['shared_login_issue']  # Warm metadata session.
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', 0.3)
    command._learn_startup_seconds(_revision(native), 1.5, reason='synthetic')
    flight = command._native_flight_seconds(_revision(native))
    assert flight == pytest.approx(3.3)
    native['auth'].write_text(json.dumps(_token(expired=True)))
    native['mode'].write_text('hang-initialize')  # Only the renewal starts native Codex.
    _expire_metadata(monkeypatch)

    assert command._lock.acquire(timeout=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        before = time.monotonic()
        pending = pool.submit(read_local_login, environment=native['env'], refresh=True)
        time.sleep(2.0)  # Discovery queues behind another native flight.
        command._lock.release()
        snapshot = pending.result(timeout=10)
        elapsed = time.monotonic() - before

    # Renewal received only the remainder: the read ends after one flight,
    # not after discovery's 2 s plus a fresh 3 s renewal startup budget.
    assert snapshot == {'shared_login_issue': 'codex_command_timeout', 'account_id': 'account-a'}
    assert elapsed < flight + 0.8, elapsed
    assert _startup_budget(native) == 3.0  # A start cut short by the flight is no evidence.
    assert all(child.poll() is not None for child in native['processes'][1:])


def test_converged_budget_fits_cold_discovery_and_cold_renewal_in_one_flight(native, monkeypatch):
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', 1.0)
    command._learn_startup_seconds(_revision(native), 0.85, reason='synthetic')
    native['mode'].write_text('slow-initialize:0.8')
    native['auth'].write_text(json.dumps(_token(expired=True)))

    before = time.monotonic()
    snapshot = read_local_login(environment=native['env'], refresh=True)
    elapsed = time.monotonic() - before

    # Twice the observed startup leaves room for the renewal's own cold start.
    assert snapshot.get('access_token'), snapshot
    assert elapsed < command._native_flight_seconds(_revision(native)) + 0.5, elapsed
    events = [event['event'] for event in _events(native)]
    assert events.count('initialize') == 2 and events.count('account/read') == 1


def test_one_fast_start_decays_but_cannot_erase_a_slow_storage_budget(monkeypatch):
    budgets = OrderedDict()
    monkeypatch.setattr(command, '_startup_budgets', budgets)
    monkeypatch.delenv('TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS', raising=False)
    # Network home (9 s), warm page cache (1 s), network home again (9 s).
    command._learn_startup_seconds('network-home', 9.0, reason='initialize_observed')
    assert command._startup_seconds('network-home') == 18.0
    command._learn_startup_seconds('network-home', 1.0, reason='initialize_observed')
    assert command._startup_seconds('network-home') == 13.5  # The next 9 s start still fits.
    command._learn_startup_seconds('network-home', 9.0, reason='initialize_observed')
    assert command._startup_seconds('network-home') == 18.0
    for _ in range(6):  # Sustained fast starts still return to the default.
        command._learn_startup_seconds('network-home', 1.0, reason='initialize_observed')
    assert 'network-home' not in budgets and command._startup_seconds('network-home') == 4.0


def test_slow_start_after_a_fast_warm_start_succeeds_first_time(native, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=command.__name__)
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', 0.5)
    command._learn_startup_seconds(_revision(native), 1.05, reason='synthetic')  # Converged.
    # The 9 s / 1 s / 9 s sequence scaled down; each read starts a new app-server.
    for mode in ('slow-initialize:1.0', '', 'slow-initialize:1.0'):
        native['mode'].write_text(mode)
        assert not _context(native)['shared_login_issue']
        command.clear_command_context()
    assert 'codex_command_timeout' not in caplog.text
    assert [event['event'] for event in _events(native)].count('initialize') == 3
