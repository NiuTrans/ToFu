"""Behavioral boundaries shared by simulation, backtest and risk reporting."""
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from tofu_trading.trading.llm_simulator import _compute_metrics
from tofu_trading.trading_backtest_engine.reporting import compute_metrics
from tofu_trading.trading_strategy_engine.risk_metrics import compute_advanced_metrics

pytestmark = pytest.mark.unit
OWNERS = ('simulation', 'backtest', 'risk')


def _curve(values):
    start = date(2023, 1, 1)
    return [{'date': (start + timedelta(days=i)).isoformat(), 'value': value}
            for i, value in enumerate(values)]


def _report(owner, curve, initial_capital=100):
    if owner == 'simulation':
        return _compute_metrics(curve, initial_capital, 0, 0, 0)
    if owner == 'risk':
        return compute_advanced_metrics(curve)
    state = SimpleNamespace(daily_values=curve, trade_log=[], positions={},
                            total_fees=0, drawdown_levels=[])
    report = compute_metrics(state, {}, [point['date'] for point in curve], initial_capital)
    return report.get('summary', report)


@pytest.mark.parametrize('owner', OWNERS)
def test_equal_peaks_and_drawdowns_retain_the_first_observed_period(owner):
    curve = _curve([100, 200, 200, 100, 200, 100, 400, 200, 200, 400])
    before = deepcopy(curve)
    report = _report(owner, curve)
    assert report['max_drawdown_pct'] == 50
    if owner == 'simulation':
        assert (report['max_dd_peak_date'], report['max_dd_trough_date']) == (
            '2023-01-02', '2023-01-04')
    else:
        assert report['max_drawdown_period'] == {
            'start': '2023-01-02', 'end': '2023-01-04'}
    if owner == 'risk':
        assert report['max_drawdown_days'] == 2
    assert curve == before


@pytest.mark.parametrize('owner', OWNERS)
def test_drawdown_starts_at_observed_equity_not_configured_capital(owner):
    curve = _curve([80] * 10)
    report = _report(owner, curve, initial_capital=100)
    assert report['max_drawdown_pct'] == 0
    assert report['total_return_pct'] == (-20 if owner == 'simulation' else 0)
    if owner == 'simulation':
        assert report['max_dd_peak_date'] == report['max_dd_trough_date'] == curve[0]['date']
    else:
        assert report['max_drawdown_period'] == {
            'start': curve[0]['date'], 'end': curve[0]['date']}


def test_calendar_population_and_trading_sample_policies_remain_distinct():
    curve = _curve([80] * 11 + [88])
    for i, point in enumerate(curve):
        point['date'] = (date(2023, 1, 1) + timedelta(days=i * 365 // 11)).isoformat()
    simulation, backtest, risk = [_report(owner, curve) for owner in OWNERS]
    assert simulation['total_return_pct'] == simulation['annualized_return_pct'] == -12
    assert simulation['sharpe_ratio'] == 1.05
    assert simulation['simulation_days'] == 365
    assert backtest['total_return_pct'] == risk['total_return_pct'] == 10
    assert backtest['annualized_return_pct'] == risk['annualized_return_pct'] == 640.02
    assert backtest['sharpe_ratio'] == risk['sharpe_ratio'] > simulation['sharpe_ratio']
    # These owners deliberately use different downside-deviation denominators.
    assert backtest['sortino_ratio'] < risk['sortino_ratio']


@pytest.mark.parametrize('owner', OWNERS)
def test_nonpositive_previous_values_preserve_observation_policy(owner):
    report = _report(owner, _curve([100, 0] * 5))
    assert report['max_drawdown_pct'] == 100
    if owner == 'simulation':
        # Zero-valued predecessors are omitted; the five remaining returns agree.
        assert report['sharpe_ratio'] == 0
        assert report['decision_points'] == 10
    else:
        assert report['sharpe_ratio'] < 0
        if owner == 'risk':
            assert report['trading_days'] == 9
            assert report['losing_days'] == 5


@pytest.mark.parametrize(('owner', 'expected'), [
    ('simulation', {}),
    ('backtest', {'error': 'No daily values recorded'}),
    ('risk', {'error': 'Insufficient data for metrics'}),
])
def test_empty_series_retains_each_entry_points_contract(owner, expected):
    assert _report(owner, []) == expected


def test_short_risk_history_is_rejected_before_reading_points():
    assert compute_advanced_metrics([{}] * 9) == {
        'error': 'Insufficient data for metrics'}


@pytest.mark.parametrize('owner', OWNERS)
def test_missing_equity_is_an_error_without_mutating_input(owner):
    curve = _curve([100] * 10)
    del curve[-1]['value']
    before = deepcopy(curve)
    with pytest.raises(KeyError) as caught:
        _report(owner, curve)
    assert caught.value.args == ('value',)
    assert curve == before


def test_invalid_simulation_date_keeps_warning_and_unannualized_return(caplog):
    curve = _curve([100] * 11 + [110])
    curve[-1]['date'] = 'unknown'
    report = _report('simulation', curve)
    assert report['annualized_return_pct'] == report['total_return_pct'] == 10
    assert report['simulation_days'] == 0
    assert 'Annualisation date parse failed' in caplog.text


@pytest.mark.parametrize(('value', 'message'), [
    (None, "'>' not supported between instances of 'NoneType' and 'NoneType'"),
    ('missing', "'>' not supported between instances of 'str' and 'int'"),
])
def test_invalid_initial_simulation_equity_preserves_first_comparison(value, message):
    curve = _curve([100] * 12)
    curve[0]['value'] = value
    with pytest.raises(TypeError) as caught:
        _report('simulation', curve)
    assert str(caught.value) == message
