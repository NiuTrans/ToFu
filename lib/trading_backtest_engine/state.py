"""lib/trading_backtest_engine/state.py — Portfolio State Management

Classes that track portfolio state during backtesting:
  - BacktestState: cash, positions, pending orders, daily values
  - StopLossManager: per-position trailing stops, stop-loss, take-profit
  - DrawdownProtector: circuit breaker based on portfolio drawdown
"""

__all__ = [
    "BacktestState",
    "StopLossManager",
    "DrawdownProtector",
]


class BacktestState:
    """Tracks portfolio state throughout backtest."""

    def __init__(self, initial_capital, all_dates, symbols):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}       # {code: {shares, cost, avg_cost_nav, entry_date, last_nav, current_value}}
        self.pending_orders = []  # T+1 orders waiting execution
        self.trade_log = []
        self.daily_values = []    # [{date, value, cash, invested}]
        self.drawdown_levels = []
        self.total_fees = 0.0
        self.all_dates = all_dates
        self.symbols = symbols

    def get_portfolio_value(self):
        return self.cash + sum(p.get('current_value', 0) for p in self.positions.values())

    def record_daily(self, date, portfolio_value):
        invested = sum(p.get('current_value', 0) for p in self.positions.values())
        self.daily_values.append({
            'date': date,
            'value': portfolio_value,
            'cash': self.cash,
            'invested': invested,
        })


class StopLossManager:
    """Manages per-position trailing stops, stop-loss, and take-profit."""

    def __init__(self):
        self.stops = {}  # {code: {entry_nav, high_nav, stop_nav, trailing_pct, take_profit_nav}}

    def register(self, code, entry_nav, risk_params):
        stop_loss_pct = risk_params.get('stop_loss_pct', 0.08)
        trailing_pct = risk_params.get('trailing_stop_pct', 0.10)
        take_profit_pct = risk_params.get('take_profit_pct', 0.30)

        self.stops[code] = {
            'entry_nav': entry_nav,
            'high_nav': entry_nav,
            'stop_nav': entry_nav * (1 - stop_loss_pct),
            'trailing_pct': trailing_pct,
            'take_profit_nav': entry_nav * (1 + take_profit_pct),
        }

    def update(self, code, current_nav, date):
        if code not in self.stops:
            return None

        s = self.stops[code]

        # Update trailing high
        if current_nav > s['high_nav']:
            s['high_nav'] = current_nav
            s['stop_nav'] = current_nav * (1 - s['trailing_pct'])

        # Check stop-loss (trailing or fixed)
        if current_nav <= s['stop_nav']:
            action = {
                'action': 'stop_loss',
                'type': 'trailing' if s['high_nav'] > s['entry_nav'] * 1.03 else 'fixed',
                'reason': f"NAV {current_nav:.4f} hit stop {s['stop_nav']:.4f} (peak {s['high_nav']:.4f})",
            }
            del self.stops[code]
            return action

        # Check take-profit (partial sell at 50%)
        if current_nav >= s['take_profit_nav']:
            action = {
                'action': 'take_profit',
                'sell_pct': 0.5,
                'reason': f"NAV {current_nav:.4f} hit take-profit {s['take_profit_nav']:.4f}",
            }
            # Raise take-profit target after partial sell
            s['take_profit_nav'] = current_nav * 1.15
            return action

        return None

    def remove(self, code):
        self.stops.pop(code, None)


class DrawdownProtector:
    """Circuit breaker based on portfolio drawdown."""

    def __init__(self, initial_capital, max_drawdown=0.20):
        self.peak = initial_capital
        self.max_dd = max_drawdown
        self.triggered = False

    def update(self, current_value):
        if current_value > self.peak:
            self.peak = current_value
            self.triggered = False

        dd = (self.peak - current_value) / self.peak if self.peak > 0 else 0

        if dd >= self.max_dd and not self.triggered:
            self.triggered = True
            return {
                'level': 'critical',
                'drawdown_pct': round(dd * 100, 2),
                'force_sell': True,
                'force_sell_pct': 0.5,  # Liquidate 50%
            }
        elif dd >= self.max_dd * 0.7:
            return {
                'level': 'warning',
                'drawdown_pct': round(dd * 100, 2),
                'force_sell': False,
                'force_sell_pct': 0,
            }
        return {
            'level': 'normal',
            'drawdown_pct': round(dd * 100, 2),
            'force_sell': False,
            'force_sell_pct': 0,
        }
