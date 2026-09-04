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

    # Transaction costs
    #
    # Fee RATES are deliberately absent: they come from
    # tofu_trading.trading.fee_book, per asset type and per trade size, which is
    # the single source of truth shared with the LLM simulator.
    #
    # `short_sell_penalty` (was 0.015 for holdings < 7 days) was REMOVED rather
    # than migrated. It was a second, hand-rolled implementation of the open-end
    # fund tiered-redemption fee, applied indiscriminately to every asset type:
    #   - On a fund held 3 days it double-charged, because fee_book's tier table
    #     already returns 1.5% for that holding period (1.5% + 1.5% = 3.0%).
    #   - On a stock held 3 days it charged 1.5% where the true cost is 0.076%,
    #     a 20x overcharge on a fee A-shares do not have at all.
    # The tier table expresses the concept correctly AND per asset type, so
    # keeping both would have guaranteed drift.
    #
    # `min_holding_days` went with it: it existed only to pick which of those
    # two rates applied.

    # Timing
    'decision_frequency': 1,       # 1 = daily decisions for signal strategies
    'min_signal_history': 60,      # days before signal-based trading starts

    # Strategy
    'strategy': 'signal_driven',
    # Aligned with the LLM simulator's default (was 10 here, 5 there). The two
    # engines are meant to be compared on identical data — a different position
    # cap silently changes diversification and per-name sizing, so any
    # quant-vs-LLM verdict drawn across that gap measures the config difference
    # as much as the strategies.
    'max_positions': 5,

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
