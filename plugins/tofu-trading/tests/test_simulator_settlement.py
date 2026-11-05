"""A simulation publishes only committed trades and retains the last settled step.

Real owner-bound connections over the atomic test repository exercise SQL and
commit faults, reopened history and observation at the event boundary.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

pytestmark = pytest.mark.unit


@pytest.fixture
def simulation(trading_connection_factory, monkeypatch):
    import lib.llm_dispatch as dispatch
    import lib.llm_dispatch.api as dispatch_api
    from tofu_trading.trading.historical_data import _ensure_sim_tables

    db = trading_connection_factory()
    _ensure_sim_tables(db)
    for day in range(1, 5):
        db.execute('INSERT INTO trading_sim_prices (symbol,date,nav,open,close) VALUES (?,?,?,?,?)',
                   ('510300', f'2024-01-0{day}', 10 + day, 10 + day, 10 + day))
    db.commit()
    buy = {'action': 'buy', 'symbol': '510300', 'amount': 10000,
           'confidence': 95, 'reason': 'scripted purchase'}
    sell = {'action': 'sell', 'symbol': '510300',
            'confidence': 95, 'reason': 'scripted close'}
    state = SimpleNamespace(db=db, decisions=[[buy], [sell], [], []], events=[],
                            prompts=[], observe=None, session_id=None, strategy_names=[])

    def decide(**kwargs):
        decisions = state.decisions[len(state.prompts)]
        state.prompts.append(kwargs)
        return ('<decisions>' + json.dumps(decisions) + '</decisions><strategies_used>'
                + json.dumps(state.strategy_names) + '</strategies_used>'), {}

    monkeypatch.setattr(dispatch, 'smart_chat', decide)
    monkeypatch.setattr(dispatch_api, 'smart_chat', decide)
    state.config = SimulatorConfig(symbols=['510300'], start_date='2024-01-01',
                                   end_date='2024-01-04', step_days=1, initial_capital=100000,
                                   adaptive_stop=False, stop_loss_pct=99, take_profit_pct=99)

    def on_event(kind, data):
        state.events.append((kind, data))
        if kind == 'sim_start':
            state.session_id = data['session_id']
        if state.observe:
            state.observe(kind, data)

    def read():
        fresh = trading_connection_factory()
        return {name: [dict(row) for row in fresh.execute('SELECT * FROM ' + name).fetchall()]
                for name in ('trading_sim_sessions', 'trading_sim_positions', 'trading_sim_journal')}

    state.read = read
    state.run = lambda: run_simulation(db, state.config, on_event, uid=1)
    return state


def inject_write_failure(monkeypatch, simulation, match, failure):
    execute = simulation.db.execute
    triggered = []

    def execute_once(sql, params=()):
        if not triggered and match(' '.join(sql.split()), params):
            triggered.append(True)
            raise failure
        return execute(sql, params)

    monkeypatch.setattr(simulation.db, 'execute', execute_once)
    return triggered


@pytest.mark.parametrize('boundary', ['buy', 'sell', 'analysis', 'decision', 'step_summary', 'progress'])
def test_required_step_write_failure_aborts_without_partial_settlement(simulation, monkeypatch, boundary):
    failure = RuntimeError('required simulation write rejected')

    def matches(sql, params):
        if boundary == 'buy':
            return sql.startswith('INSERT INTO trading_sim_positions')
        if boundary == 'sell':
            return sql.startswith('UPDATE trading_sim_positions')
        if boundary == 'progress':
            return sql.startswith('UPDATE trading_sim_sessions SET current_cash=')
        return sql.startswith('INSERT INTO trading_sim_journal') and params[2] == boundary

    injected = inject_write_failure(monkeypatch, simulation, matches, failure)
    with pytest.raises(RuntimeError) as raised:
        simulation.run()
    assert raised.value is failure
    assert injected
    rows = simulation.read()
    session = rows['trading_sim_sessions'][0]
    expected_steps = 1 if boundary == 'sell' else 0
    assert session['status'] == 'failed'
    assert session['completed_steps'] == expected_steps
    assert session['current_cash'] == (90000 if expected_steps else 100000)
    assert session['total_trades'] == 0
    assert [row['status'] for row in rows['trading_sim_positions']] == (['open'] if expected_steps else [])
    assert {row['sim_date'] for row in rows['trading_sim_journal']} == (
        {'2024-01-01'} if expected_steps else set())
    assert len([1 for kind, _ in simulation.events if kind == 'sim_trade']) == expected_steps
    assert len([1 for kind, _ in simulation.events if kind == 'sim_step_done']) == expected_steps
    assert all(kind != 'sim_complete' for kind, _ in simulation.events)


@pytest.mark.parametrize('phase', ['step', 'final'])
@pytest.mark.parametrize('applied', [False, True])
def test_commit_failure_is_not_replayed_or_reported_as_success(
        simulation, trading_repository, monkeypatch, phase, applied):
    original_batch = trading_repository.batch
    failure = RuntimeError('commit acknowledgement unavailable')
    injected = []

    def batch(mutations):
        sessions = [item['document']['row'] for item in mutations
                    if item.get('document', {}).get('logical_table') == 'trading_sim_sessions']
        target = any(row['status'] == 'completed' if phase == 'final'
                     else row['status'] == 'running' and row['completed_steps'] == 2 for row in sessions)
        if target and not injected:
            injected.append(mutations)
            if applied:
                original_batch(mutations)
            raise failure
        return original_batch(mutations)

    monkeypatch.setattr(trading_repository, 'batch', batch)
    with pytest.raises(RuntimeError) as raised:
        simulation.run()
    assert raised.value is failure
    assert len(injected) == 1
    rows = simulation.read()
    session = rows['trading_sim_sessions'][0]
    assert session['status'] == ('completed' if applied and phase == 'final' else 'failed')
    assert session['completed_steps'] == (4 if phase == 'final' else 2 if applied else 1)
    assert len(rows['trading_sim_positions']) == 1
    assert rows['trading_sim_positions'][0]['status'] == ('open' if phase == 'step' and not applied else 'closed')
    assert len([1 for kind, _ in simulation.events if kind == 'sim_step_done']) == (4 if phase == 'final' else 1)
    assert all(kind != 'sim_complete' for kind, _ in simulation.events)
    summaries = [row for row in rows['trading_sim_journal'] if row['entry_type'] == 'step_summary']
    assert len(summaries) == session['completed_steps']


def test_committed_events_can_be_replayed_from_a_fresh_connection(simulation):
    snapshots = []

    def observe(kind, data):
        if kind in ('sim_trade', 'sim_step_done', 'sim_complete'):
            snapshots.append((kind, data, simulation.read()))

    simulation.observe = observe
    result = simulation.run()
    assert result['status'] == 'completed'
    assert len(snapshots) == 7
    for kind, data, rows in snapshots:
        session = rows['trading_sim_sessions'][0]
        if kind == 'sim_complete':
            assert session['status'] == 'completed'
            assert json.loads(session['result_json']) == result
            continue
        assert session['completed_steps'] == data['step']
        summaries = [row for row in rows['trading_sim_journal']
                     if row['entry_type'] == 'step_summary' and row['sim_date'] == data['sim_date']]
        assert len(summaries) == 1
        if kind == 'sim_trade':
            assert rows['trading_sim_positions'][0]['status'] == ('open' if data['action'] == 'buy' else 'closed')
        else:
            summary = json.loads(summaries[0]['signals_json'])
            assert summary['cash'] == data['cash']
            assert summary['actions'] == data['actions']


def test_recoverable_decision_failure_discards_all_unsettled_step_state(simulation):
    simulation.decisions[0].append({'action': 'buy', 'symbol': '510500',
                                     'confidence': 95, 'amount': 'malformed'})
    result = simulation.run()
    rows = simulation.read()
    assert result['status'] == 'completed'
    assert result['trade_log'] == []
    assert result['total_fees'] == 0
    assert result['metrics']['final_value'] == 100000
    assert rows['trading_sim_positions'] == []
    assert all(row['sim_date'] != '2024-01-01' for row in rows['trading_sim_journal'])
    assert [entry['date'] for entry in result['daily_values']] == [
        '2024-01-02', '2024-01-03', '2024-01-04', '2024-01-04']
    assert all(kind != 'sim_trade' for kind, _ in simulation.events)
    assert len([1 for kind, _ in simulation.events if kind == 'sim_error']) == 1


@pytest.mark.parametrize('boundary', ['create', 'liquidation', 'result'])
def test_session_boundaries_never_publish_uncommitted_completion(simulation, monkeypatch, boundary):
    simulation.decisions[1] = []
    failure = RuntimeError('session boundary failed')

    def matches(sql, params):
        return (sql.startswith('INSERT INTO trading_sim_sessions') if boundary == 'create' else
                sql.startswith('UPDATE trading_sim_positions') if boundary == 'liquidation' else
                sql.startswith("UPDATE trading_sim_sessions SET status='completed'"))

    inject_write_failure(monkeypatch, simulation, matches, failure)
    with pytest.raises(RuntimeError) as raised:
        simulation.run()
    assert raised.value is failure
    rows = simulation.read()
    if boundary == 'create':
        assert rows['trading_sim_sessions'] == []
        assert simulation.events == []
    else:
        assert rows['trading_sim_sessions'][0]['status'] == 'failed'
        assert rows['trading_sim_sessions'][0]['current_cash'] == 90000
        assert rows['trading_sim_positions'][0]['status'] == 'open'
    assert all(kind != 'sim_complete' for kind, _ in simulation.events)


@pytest.mark.parametrize('diagnostic_failure', [False, True])
def test_failed_marker_and_unprintable_exception_preserve_the_original_failure(
        simulation, monkeypatch, diagnostic_failure):
    from tofu_trading.trading import llm_simulator as engine
    class UnprintableFailure(RuntimeError):
        def __str__(self):
            raise ValueError('broken exception formatter')

    failure = UnprintableFailure()
    execute = simulation.db.execute

    def reject(sql, params=()):
        if sql.lstrip().startswith('UPDATE trading_sim_positions'):
            raise failure
        if "status='failed'" in sql:
            raise RuntimeError('failure marker unavailable')
        return execute(sql, params)

    monkeypatch.setattr(simulation.db, 'execute', reject)
    if diagnostic_failure:
        def broken_logger(*args, **kwargs):
            raise RuntimeError('diagnostic unavailable')
        monkeypatch.setattr(engine.logger, 'exception', broken_logger)
    with pytest.raises(UnprintableFailure) as raised:
        simulation.run()
    assert raised.value is failure
    assert simulation.read()['trading_sim_positions'][0]['status'] == 'open'
    assert all(kind != 'sim_complete' for kind, _ in simulation.events)


def test_observer_failure_does_not_undo_or_repeat_settlement(simulation):
    def broken(kind, data):
        if kind in ('sim_trade', 'sim_step_done', 'sim_complete'):
            raise RuntimeError('observer unavailable')

    simulation.observe = broken
    result = simulation.run()
    rows = simulation.read()
    assert result['status'] == rows['trading_sim_sessions'][0]['status'] == 'completed'
    assert len(rows['trading_sim_positions']) == 1
    assert rows['trading_sim_positions'][0]['status'] == 'closed'
    assert len([1 for kind, _ in simulation.events if kind == 'sim_complete']) == 1


@pytest.mark.parametrize('recoverable_failure', [False, True])
def test_price_cache_commit_cannot_flush_unsettled_trades(simulation, monkeypatch, recoverable_failure):
    from tofu_trading.trading import llm_simulator as engine

    snapshots = []

    def acquire(db, symbol, start, end, emit, step):
        for day in range(1, 5):
            db.execute('INSERT INTO trading_sim_prices (symbol,date,nav,open,close) VALUES (?,?,?,?,?)',
                       (symbol, f'2024-01-0{day}', 10.0, 10.0, 10.0))
        db.commit()
        emit('sim_fetching_symbol', {'symbol': symbol})
        snapshots.append(simulation.read())
        return True

    monkeypatch.setattr(engine, '_ensure_price_data', acquire)
    monkeypatch.setattr(engine, '_resolve_symbol_name', lambda symbol: symbol)
    simulation.decisions[0].append({'action': 'buy', 'symbol': '510500',
                                     'confidence': 95, 'amount': 10000})
    if recoverable_failure:
        simulation.decisions[0].append({'action': 'buy', 'symbol': '510050',
                                         'confidence': 95, 'amount': 'invalid'})
    result = simulation.run()
    assert len(snapshots) == 1
    before_commit = snapshots[0]
    assert before_commit['trading_sim_sessions'][0]['completed_steps'] == 0
    assert before_commit['trading_sim_sessions'][0]['current_cash'] == 100000
    assert before_commit['trading_sim_positions'] == []
    assert before_commit['trading_sim_journal'] == []
    assert simulation.db.execute("SELECT COUNT(*) FROM trading_sim_prices WHERE symbol='510500'").fetchone()[0] == 4
    assert result['metrics']['total_trades'] == (0 if recoverable_failure else 2)
    assert simulation.config.symbols == (['510300'] if recoverable_failure else ['510300', '510500'])


def test_strategy_feedback_is_required_in_the_same_sell_settlement(simulation, monkeypatch):
    from tofu_trading.trading import llm_simulator as engine

    monkeypatch.setattr(engine, '_load_strategy_toolbox', lambda db, uid: (
        'Scripted strategy', [{'id': 1, 'name': 'scripted'}]))
    simulation.strategy_names = ['scripted']
    failure = RuntimeError('strategy feedback unavailable')
    injected = inject_write_failure(monkeypatch, simulation,
                                    lambda sql, params: sql.startswith('INSERT INTO trading_strategy_performance'),
                                    failure)
    with pytest.raises(RuntimeError) as raised:
        simulation.run()
    assert raised.value is failure
    assert injected
    rows = simulation.read()
    assert rows['trading_sim_sessions'][0]['status'] == 'failed'
    assert rows['trading_sim_sessions'][0]['total_trades'] == 0
    assert rows['trading_sim_positions'][0]['status'] == 'open'
    assert all(data.get('action') != 'sell' for kind, data in simulation.events if kind == 'sim_trade')


@pytest.mark.parametrize('exit_type', [KeyboardInterrupt, SystemExit])
def test_process_control_exit_is_preserved_and_does_not_publish_success(simulation, monkeypatch, exit_type):
    failure = exit_type('stop simulation')
    inject_write_failure(monkeypatch, simulation,
                         lambda sql, params: sql.startswith('UPDATE trading_sim_positions'), failure)
    with pytest.raises(exit_type) as raised:
        simulation.run()
    assert raised.value is failure
    rows = simulation.read()
    assert rows['trading_sim_sessions'][0]['status'] == 'failed'
    assert rows['trading_sim_positions'][0]['status'] == 'open'
    assert all(kind != 'sim_complete' for kind, _ in simulation.events)


def test_shared_task_runtime_receives_the_original_settlement_failure(simulation, monkeypatch):
    import asyncio
    import sys
    import quart

    monkeypatch.setitem(sys.modules, 'flask', quart)
    from tofu_trading.web.handlers import trading_simulator as route

    failure = RuntimeError('position write unavailable')
    injected = inject_write_failure(monkeypatch, simulation,
                                    lambda sql, params: sql.startswith('UPDATE trading_sim_positions'), failure)
    monkeypatch.setattr(route, 'current_user_id', lambda: 1)
    monkeypatch.setattr(route, 'get_thread_db', lambda *args, **kwargs: simulation.db)
    monkeypatch.setattr(route.threading, 'Thread',
                        lambda *, target, **kwargs: SimpleNamespace(start=target))
    app = quart.Quart(__name__)

    async def request():
        async with app.test_request_context('/api/trading/sim/run', method='POST', json={
                'symbols': ['510300'], 'start_date': '2024-01-01', 'end_date': '2024-01-04',
                'step_days': 1, 'stop_loss_pct': 99, 'take_profit_pct': 99}):
            response = await route.sim_run()
            return await response.get_json()

    response = asyncio.run(request())
    progress = route._get_task_progress(response['task_id'], 0, owner_user_id=1)
    assert injected
    assert progress['done'] is True
    assert progress.get('error')
    assert 'result' not in progress
    assert not [event for event in progress['events'] if event.get('_type') == 'sim_complete']
    assert simulation.read()['trading_sim_sessions'][0]['status'] == 'failed'


def test_rollback_failure_preserves_the_original_write_failure():
    from tofu_trading.transactions import write_transaction

    failure = RuntimeError('write rejected')

    class BrokenRollback:
        def begin(self):
            pass

        def rollback(self):
            raise ValueError('rollback unavailable')

    with pytest.raises(RuntimeError) as raised:
        with write_transaction(BrokenRollback()):
            raise failure
    assert raised.value is failure
