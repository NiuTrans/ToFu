"""lib/trading_backtest_engine/config.py — Default Configuration & Constants

Centralises all default backtest configuration values and engine constants.
"""

__all__ = [
    "DEFAULT_CONFIG",
    "STRATEGY_NAMES",
    "ALL_STRATEGIES",
]

# ── Default engine configuration ──────────────────────────
DEFAULT_CONFIG = {
    # Capital
    'initial_capital': 100_000,

    # Transaction costs (market)
    'buy_fee_rate': 0.0015,
    'sell_fee_rate': 0.005,
    'short_sell_penalty': 0.015,  # < 7 days holding

    # Timing
    'decision_frequency': 1,       # 1 = daily decisions for signal strategies
    'min_signal_history': 60,      # days before signal-based trading starts
    'min_holding_days': 7,         # trading rule

    # Strategy
    'strategy': 'signal_driven',
    'max_positions': 10,

    # Risk management
    'enable_stop_loss': True,
    'enable_drawdown_protection': True,

    # v2 recalibrated signal thresholds — composite score range ~[-50, +50]
    # Weighted score rarely exceeds ±60 (each component capped, weighted 25%×80 max = 20)
    'buy_threshold': 8,
    'strong_buy_threshold': 20,
    'sell_threshold': -8,
    'strong_sell_threshold': -20,

    # DCA settings
    'dca_amount': 2000,
    'dca_signal_boost': 1.5,

    # Risk-free rate for Sharpe/Sortino (CNY benchmark)
    'risk_free_rate': 0.025,
}

# ── Strategy display names (Chinese) ─────────────────────
STRATEGY_NAMES = {
    'buy_and_hold': '买入持有',
    'dca': '定投',
    'signal_driven': '信号驱动',
    'dca_signal': '智能定投',
    'mean_reversion': '均值回归',
    'trend_following': '趋势跟踪',
    'adaptive': '自适应',
}

# ── All supported strategies ──────────────────────────────
ALL_STRATEGIES = list(STRATEGY_NAMES.keys())
