"""Simulation outcomes follow executed fills, including gaps after risk triggers.

Exercise the complete engine with the production owner-bound storage adapter,
fixed bars and scripted model decisions. No market or model service is called.
"""
from __future__ import annotations

import json

import pytest

from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

pytestmark = pytest.mark.unit


@pytest.fixture
def gap_simulation(trading_connection_factory, monkeypatch):
    import lib.llm_dispatch as dispatch
    import lib.llm_dispatch.api as dispatch_api
    from tofu_trading.trading.historical_data import _ensure_sim_tables

    def run(trigger, fill, *, event_action=None, decision_observer=None):
        db = trading_connection_factory(isolated=True)
        _ensure_sim_tables(db)
        trigger_close = 9.0 if trigger == 'stop_loss' else 11.0
        bars = [('2024-01-01', 10.0, 10.0),
                ('2024-01-02', 10.0, trigger_close),
                ('2024-01-03', fill, fill)]
        for date, opening, closing in bars:
            db.execute('INSERT INTO trading_sim_prices (symbol,date,nav,open,close) VALUES (?,?,?,?,?)',
                       ('510300', date, closing, opening, closing))
        db.commit()
        calls = []

        def decide(**kwargs):
            calls.append(kwargs)
            if decision_observer:
                decision_observer(kwargs)
            decisions = ([{'action': 'buy', 'symbol': '510300', 'amount': 10000,
                           'confidence': 95, 'reason': 'fixed purchase'}] if len(calls) == 1 else [])
            return '<decisions>' + json.dumps(decisions) + '</decisions>', {}

        monkeypatch.setattr(dispatch, 'smart_chat', decide)
        monkeypatch.setattr(dispatch_api, 'smart_chat', decide)
        config = SimulatorConfig(symbols=['510300'], start_date=bars[0][0], end_date=bars[-1][0],
                                 step_days=1, initial_capital=100000, adaptive_stop=False,
                                 stop_loss_pct=5, take_profit_pct=5)
        events = []
        def on_event(kind, data):
            events.append((kind, data))
            if event_action:
                event_action(config, kind, data)

        result = run_simulation(db, config, on_event, uid=1)
        assert len(calls) == 3
        assert not [event for event in events if event[0] == 'sim_error']
        return db, config, result, events

    return run


def _current_holdings(request):
    content = next(message['content'] for message in request['messages']
                   if message['role'] == 'user')
    return content.split('## 当前持仓\n', 1)[1].split('\n\n## 可交易标的', 1)[0]


@pytest.mark.parametrize('trigger', ['stop_loss', 'take_profit'])
def test_model_holdings_match_analysis_after_automatic_exit(gap_simulation, trigger):
    requests = []
    _, _, result, events = gap_simulation(trigger, 9.0, decision_observer=requests.append)
    analysis = [data for kind, data in events if kind == 'sim_analyzing']
    assert result['trade_log'][1]['trigger'] == trigger
    assert analysis[1]['holdings'] == []
    assert _current_holdings(requests[1]) == '（无持仓）'


def test_model_keeps_a_position_when_risk_exit_is_not_triggered(gap_simulation):
    requests = []
    def disable_exit(config, kind, data):
        if kind == 'sim_step' and data['step'] == 2:
            config.stop_loss_pct = 99
            config.take_profit_pct = 999

    _, _, _, events = gap_simulation('stop_loss', 9.0, event_action=disable_exit,
                                     decision_observer=requests.append)
    analysis = [data for kind, data in events if kind == 'sim_analyzing']
    assert analysis[1]['holdings'][0]['symbol'] == '510300'
    assert '- 510300 ' in _current_holdings(requests[1])


def test_missing_quote_keeps_known_holdings_in_model_context(gap_simulation, monkeypatch):
    from tofu_trading.trading import historical_data

    get_price = historical_data.get_price_at
    monkeypatch.setattr(historical_data, 'get_price_at',
                        lambda db, symbol, date: None if date == '2024-01-02'
                        else get_price(db, symbol, date))
    requests = []
    _, _, _, events = gap_simulation('stop_loss', 9.0, decision_observer=requests.append)
    analysis = [data for kind, data in events if kind == 'sim_analyzing']
    assert analysis[1]['holdings'][0]['symbol'] == '510300'
    assert analysis[1]['holdings'][0]['pnl_pct'] == 0
    holdings = _current_holdings(requests[1])
    assert '- 510300 ' in holdings
    assert '现价¥10.0000' in holdings  # Same last known mark used by the ledger.


@pytest.mark.parametrize('trigger', ['stop_loss', 'take_profit'])
@pytest.mark.parametrize('fill', [9.0, 10.0, 11.0])
def test_risk_exit_wins_follow_fill_profit_not_trigger(gap_simulation, trigger, fill):
    db, config, result, events = gap_simulation(trigger, fill)
    buys = [trade for trade in result['trade_log'] if trade['action'] == 'buy']
    sells = [trade for trade in result['trade_log'] if trade['action'] == 'sell']
    assert len(buys) == len(sells) == 1
    assert buys[0]['price'] == 10.0
    assert sells[0]['trigger'] == trigger
    assert sells[0]['price'] == fill
    assert sells[0]['pnl_pct'] == pytest.approx((fill - 10.0) / 10.0 * 100)
    steps = [data for kind, data in events if kind == 'sim_step_done']
    assert [[action['action'] for action in step['actions']] for step in steps] == [['buy'], ['sell'], []]
    expected_wins = int(fill > 10.0)
    assert result['metrics']['total_trades'] == 1
    assert result['metrics']['winning_trades'] == expected_wins
    assert result['metrics']['win_rate'] == expected_wins * 100
    session = db.execute('SELECT * FROM trading_sim_sessions WHERE session_id=?',
                         (result['session_id'],)).fetchone()
    assert session['winning_trades'] == expected_wins
    assert session['total_trades'] == 1
    buy_fee = config.fee_book.fee_for('510300', 10000, 'buy')['fee_amount']
    shares = (10000 - buy_fee) / 10.0
    sale_value = shares * fill
    sell_fee = config.fee_book.fee_for('510300', sale_value, 'sell', holding_days=1)['fee_amount']
    expected_cash = 90000 + sale_value - sell_fee
    assert session['current_cash'] == pytest.approx(expected_cash)
    assert result['metrics']['final_value'] == round(expected_cash, 2)
    assert result['total_fees'] == round(buy_fee + sell_fee, 2)
    assert len([event for kind, event in events if kind == 'sim_trade' and event['action'] == 'sell']) == 1


def test_fee_schedule_replacement_applies_to_subsequent_fills(gap_simulation):
    from tofu_trading.trading.fee_book import FeeBook

    def replace_fee_book(config, kind, data):
        if kind == 'sim_step' and data['step'] == 2:
            config.fee_book = FeeBook(zero_cost=True)

    _, _, result, _ = gap_simulation('stop_loss', 9.0, event_action=replace_fee_book)
    purchase_fee = FeeBook().fee_for('510300', 10000, 'buy')['fee_amount']
    assert purchase_fee > 0
    assert result['total_fees'] == round(purchase_fee, 2)


@pytest.mark.parametrize('action', ['buy', 'sell'])
def test_fee_failure_does_not_book_a_trade(monkeypatch, action):
    from tofu_trading.trading.fee_book import FeeBook
    from tofu_trading.trading.simulation_ledger import SimulationLedger

    fee_book = FeeBook()
    ledger = SimulationLedger(100000)
    shares = ledger.buy('510300', 10000, 10.0, fee_book=fee_book)
    before = (ledger.cash, ledger.total_fees, ledger.total_trades, ledger.winning_trades)

    def unavailable(*args, **kwargs):
        raise RuntimeError('fee unavailable')

    monkeypatch.setattr(fee_book, 'fee_for', unavailable)
    with pytest.raises(RuntimeError, match='fee unavailable'):
        if action == 'buy':
            ledger.buy('510300', 10000, 10.0, fee_book=fee_book)
        else:
            ledger.sell('510300', {'shares': shares, 'buy_price': 10.0}, 11.0,
                        fee_book=fee_book, holding_days=1)
    assert (ledger.cash, ledger.total_fees, ledger.total_trades, ledger.winning_trades) == before
