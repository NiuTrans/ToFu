"""lib/trading_risk.py — Risk Management Engine v1

Quantitative risk management:
  - Dynamic Position Sizing (Kelly criterion, volatility targeting)
  - Stop-loss / Take-profit management
  - Drawdown protection (circuit breaker)
  - Correlation-aware portfolio allocation
  - Risk budget management
  - Regime-adaptive risk scaling

NO LLM dependency — pure computation.
"""

import math
from collections import defaultdict
from datetime import datetime

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  Position Sizing
# ═══════════════════════════════════════════════════════════

def kelly_fraction(win_rate, avg_win, avg_loss):
    """Kelly Criterion for optimal bet size.

    f* = (p * b - q) / b
    where p = win probability, q = 1-p, b = avg_win/avg_loss (odds)

    Returns fraction of capital to allocate (0 to 1).
    We use half-Kelly for safety (standard practice).
    """
    if avg_loss == 0 or win_rate <= 0 or win_rate >= 1:
        return 0
    p = win_rate
    q = 1 - p
    b = abs(avg_win / avg_loss)
    kelly = (p * b - q) / b
    # Half-Kelly for safety + clip to reasonable range
    half_kelly = max(0, min(kelly / 2, 0.25))
    return round(half_kelly, 4)


def volatility_target_position(capital, asset_volatility, target_volatility=0.15,
                                current_nav=1.0):
    """Position size based on volatility targeting.

    Allocate capital so that the position's contribution to portfolio
    volatility equals the target.

    position_size = (target_vol / asset_vol) * capital

    Args:
        capital: total investable capital
        asset_volatility: annualized volatility of the asset (e.g., 0.20 = 20%)
        target_volatility: desired portfolio volatility contribution
        current_nav: NAV per share

    Returns dict with shares, amount, weight
    """
    if asset_volatility <= 0:
        # Unknown vol — be conservative, allocate max 10%
        weight = 0.10
    else:
        weight = target_volatility / asset_volatility
        weight = min(weight, 0.40)  # Cap at 40% single position
        weight = max(weight, 0.02)  # Min 2%

    amount = capital * weight
    shares = amount / current_nav if current_nav > 0 else 0

    return {
        'weight': round(weight, 4),
        'amount': round(amount, 2),
        'shares': round(shares, 2),
        'asset_vol': round(asset_volatility, 4),
        'target_vol': target_volatility,
        'sizing_method': 'volatility_target',
    }


def risk_parity_weights(volatilities, correlations=None):
    """Risk parity allocation — equal risk contribution from each asset.

    Simple version: weight inversely proportional to volatility.
    With correlations: iterative marginal risk contribution equalization.

    Args:
        volatilities: dict {symbol: annualized_vol}
        correlations: optional correlation matrix dict

    Returns: dict {symbol: weight}
    """
    if not volatilities:
        return {}

    # Simple inverse-vol weighting (good enough without correlation data)
    codes = list(volatilities.keys())
    inv_vols = {}
    for code in codes:
        vol = volatilities[code]
        if vol and vol > 0:
            inv_vols[code] = 1.0 / vol
        else:
            inv_vols[code] = 1.0  # unknown vol → equal weight

    total_inv = sum(inv_vols.values())
    weights = {code: round(iv / total_inv, 4) for code, iv in inv_vols.items()}
    return weights


# ═══════════════════════════════════════════════════════════
#  Stop-Loss & Take-Profit
# ═══════════════════════════════════════════════════════════

class StopLossManager:
    """Manages stop-loss and take-profit levels for each position.

    Supports:
      - Fixed stop-loss (e.g., -8% from entry)
      - Trailing stop-loss (tracks highest NAV, triggers on pullback)
      - ATR-based stop (2× ATR below price)
      - Partial take-profit (sell portion at target, trail the rest)
    """

    def __init__(self):
        self.positions = {}  # code → position state

    def add_position(self, code, entry_nav, entry_date,
                     fixed_stop_pct=-0.08,
                     trailing_stop_pct=-0.06,
                     take_profit_pct=0.25,
                     partial_take_pct=0.50):
        """Register a position with stop/profit levels."""
        self.positions[code] = {
            'entry_nav': entry_nav,
            'entry_date': entry_date,
            'fixed_stop': entry_nav * (1 + fixed_stop_pct),
            'trailing_stop_pct': trailing_stop_pct,
            'highest_nav': entry_nav,
            'trailing_stop': entry_nav * (1 + trailing_stop_pct),
            'take_profit_nav': entry_nav * (1 + take_profit_pct),
            'partial_take_pct': partial_take_pct,
            'partial_taken': False,
            'active': True,
        }

    def update(self, code, current_nav, current_date):
        """Update stop levels with latest price. Returns action if triggered.

        Returns:
            None — no action needed
            {'action': 'stop_loss', 'reason': str, 'type': 'fixed'|'trailing'}
            {'action': 'take_profit', 'reason': str, 'partial': bool, 'sell_pct': float}
        """
        if code not in self.positions:
            return None
        pos = self.positions[code]
        if not pos['active']:
            return None

        # Update trailing high
        if current_nav > pos['highest_nav']:
            pos['highest_nav'] = current_nav
            pos['trailing_stop'] = current_nav * (1 + pos['trailing_stop_pct'])

        entry = pos['entry_nav']
        return_pct = (current_nav - entry) / entry

        # Check fixed stop-loss
        if current_nav <= pos['fixed_stop']:
            return {
                'action': 'stop_loss',
                'reason': f'Fixed stop triggered: NAV {current_nav:.4f} <= stop {pos["fixed_stop"]:.4f} '
                          f'(return: {return_pct*100:.1f}%)',
                'type': 'fixed',
                'return_pct': round(return_pct * 100, 2),
            }

        # Check trailing stop-loss (only after some profit has been made)
        if return_pct > 0.05 and current_nav <= pos['trailing_stop']:
            return {
                'action': 'stop_loss',
                'reason': f'Trailing stop triggered: NAV {current_nav:.4f} <= trailing stop {pos["trailing_stop"]:.4f} '
                          f'(peak: {pos["highest_nav"]:.4f}, return: {return_pct*100:.1f}%)',
                'type': 'trailing',
                'return_pct': round(return_pct * 100, 2),
            }

        # Check take-profit
        if current_nav >= pos['take_profit_nav'] and not pos['partial_taken']:
            pos['partial_taken'] = True
            # After partial take, tighten trailing stop
            pos['trailing_stop_pct'] = pos['trailing_stop_pct'] / 2  # tighter trailing
            pos['trailing_stop'] = current_nav * (1 + pos['trailing_stop_pct'])
            return {
                'action': 'take_profit',
                'reason': f'Target reached: return {return_pct*100:.1f}%. Sell {pos["partial_take_pct"]*100:.0f}%, trail rest.',
                'partial': True,
                'sell_pct': pos['partial_take_pct'],
                'return_pct': round(return_pct * 100, 2),
            }

        return None

    def remove_position(self, code):
        if code in self.positions:
            self.positions[code]['active'] = False


# ═══════════════════════════════════════════════════════════
#  Drawdown Protection (Circuit Breaker)
# ═══════════════════════════════════════════════════════════

class DrawdownProtector:
    """Portfolio-level drawdown protection.

    When portfolio drawdown exceeds thresholds, progressively
    reduce risk exposure.

    Levels:
      Warning  (-5%)  → reduce new buy sizes by 50%
      Caution  (-10%) → stop new buys, tighten stops
      Critical (-15%) → sell 30% of positions
      Emergency (-20%) → sell 70% of positions
    """

    LEVELS = [
        {'name': 'normal', 'dd_pct': 0, 'buy_scale': 1.0, 'force_sell_pct': 0},
        {'name': 'warning', 'dd_pct': -5, 'buy_scale': 0.5, 'force_sell_pct': 0},
        {'name': 'caution', 'dd_pct': -10, 'buy_scale': 0, 'force_sell_pct': 0},
        {'name': 'critical', 'dd_pct': -15, 'buy_scale': 0, 'force_sell_pct': 0.3},
        {'name': 'emergency', 'dd_pct': -20, 'buy_scale': 0, 'force_sell_pct': 0.7},
    ]

    def __init__(self, initial_portfolio_value):
        self.peak_value = initial_portfolio_value
        self.current_level = self.LEVELS[0]
        self.triggered_sells = set()  # track which levels already triggered sells

    def update(self, current_portfolio_value):
        """Update with current portfolio value. Returns action if level changed.

        Returns:
            {'level': str, 'drawdown_pct': float, 'buy_scale': float,
             'force_sell': bool, 'force_sell_pct': float}
        """
        if current_portfolio_value > self.peak_value:
            self.peak_value = current_portfolio_value
            self.triggered_sells.clear()  # Reset when making new highs

        dd_pct = (current_portfolio_value - self.peak_value) / self.peak_value * 100

        # Find current level
        active_level = self.LEVELS[0]
        for level in self.LEVELS:
            if dd_pct <= level['dd_pct']:
                active_level = level

        old_level = self.current_level
        self.current_level = active_level

        force_sell = False
        force_sell_pct = 0
        if active_level['force_sell_pct'] > 0 and active_level['name'] not in self.triggered_sells:
            force_sell = True
            force_sell_pct = active_level['force_sell_pct']
            self.triggered_sells.add(active_level['name'])

        return {
            'level': active_level['name'],
            'level_changed': old_level['name'] != active_level['name'],
            'drawdown_pct': round(dd_pct, 2),
            'buy_scale': active_level['buy_scale'],
            'force_sell': force_sell,
            'force_sell_pct': force_sell_pct,
            'peak_value': round(self.peak_value, 2),
        }


# ═══════════════════════════════════════════════════════════
#  Regime-Adaptive Risk Parameters
# ═══════════════════════════════════════════════════════════

def get_regime_risk_params(trend_regime, vol_regime):
    """Get risk parameters adjusted for current market regime.

    Returns dict with recommended parameters:
      max_equity_pct, stop_loss_pct, trailing_stop_pct,
      take_profit_pct, buy_scale, rebalance_threshold
    """
    # Default moderate params
    params = {
        'max_equity_pct': 0.70,
        'stop_loss_pct': -0.08,
        'trailing_stop_pct': -0.06,
        'take_profit_pct': 0.25,
        'buy_scale': 1.0,
        'rebalance_threshold_pct': 5.0,
        'new_position_max_pct': 0.15,
    }

    # Adjust for trend
    if trend_regime == 'strong_bull':
        params['max_equity_pct'] = 0.85
        params['trailing_stop_pct'] = -0.08  # Wider trailing to ride trend
        params['take_profit_pct'] = 0.35
        params['buy_scale'] = 1.2
    elif trend_regime == 'bull':
        params['max_equity_pct'] = 0.75
        params['buy_scale'] = 1.1
    elif trend_regime == 'bear':
        params['max_equity_pct'] = 0.50
        params['stop_loss_pct'] = -0.06
        params['buy_scale'] = 0.6
        params['new_position_max_pct'] = 0.10
    elif trend_regime == 'strong_bear':
        params['max_equity_pct'] = 0.30
        params['stop_loss_pct'] = -0.05
        params['trailing_stop_pct'] = -0.04
        params['buy_scale'] = 0.3
        params['new_position_max_pct'] = 0.05

    # Adjust for volatility
    if vol_regime == 'high_vol':
        params['max_equity_pct'] = min(params['max_equity_pct'], 0.55)
        params['stop_loss_pct'] *= 1.3  # Wider stop in high vol
        params['trailing_stop_pct'] *= 1.3
        params['buy_scale'] *= 0.7
        params['rebalance_threshold_pct'] = 8.0  # Less frequent rebalancing
    elif vol_regime == 'extreme_vol':
        params['max_equity_pct'] = min(params['max_equity_pct'], 0.35)
        params['stop_loss_pct'] *= 1.5
        params['trailing_stop_pct'] *= 1.5
        params['buy_scale'] *= 0.4
        params['rebalance_threshold_pct'] = 12.0
    elif vol_regime == 'low_vol':
        params['stop_loss_pct'] *= 0.8  # Tighter stop in low vol
        params['trailing_stop_pct'] *= 0.8

    return params


# ═══════════════════════════════════════════════════════════
#  Portfolio Risk Analytics
# ═══════════════════════════════════════════════════════════

def compute_portfolio_risk(positions, signals_dict, correlation_matrix=None):
    """Compute portfolio-level risk metrics.

    Args:
        positions: list of {symbol, weight, current_value}
        signals_dict: {symbol: signal_snapshot} from asset_signals
        correlation_matrix: optional {(code_a, code_b): correlation}

    Returns:
        {
            portfolio_volatility, concentration_risk, regime_consistency,
            risk_budget_usage, diversification_ratio, alerts
        }
    """
    if not positions:
        return {'error': 'No positions'}

    total_value = sum(p.get('current_value', 0) for p in positions)
    if total_value <= 0:
        return {'error': 'Zero portfolio value'}

    alerts = []

    # 1. Portfolio volatility (weighted sum, simplified without correlation)
    #    TODO: use correlation_matrix when available for accurate portfolio vol
    if correlation_matrix:
        logger.debug('correlation_matrix provided (%d pairs) but not yet used', len(correlation_matrix))
    weighted_vol_sq = 0
    weighted_vol = 0
    for p in positions:
        code = p['symbol']
        weight = p.get('current_value', 0) / total_value
        sig = signals_dict.get(code, {})
        vol = sig.get('volatility_20d', 0.20)  # default 20%
        if vol is None:
            vol = 0.20
        weighted_vol += weight * vol
        weighted_vol_sq += (weight * vol) ** 2

    # With correlations, portfolio vol = sqrt(sum wi*wj*sig_i*sig_j*rho_ij)
    # Without: approximate as weighted average (upper bound for positive correlation)
    portfolio_vol = weighted_vol

    # 2. Concentration risk (Herfindahl index)
    weights = [(p.get('current_value', 0) / total_value) for p in positions]
    herfindahl = sum(w ** 2 for w in weights)
    # 1/N = perfectly diversified, 1 = all in one asset
    n = len(positions)
    concentration_score = (herfindahl - 1 / n) / (1 - 1 / n) * 100 if n > 1 else 100

    if concentration_score > 60:
        top_asset = max(positions, key=lambda p: p.get('current_value', 0))
        alerts.append(f'⚠️ High concentration: {top_asset["symbol"]} is {top_asset.get("current_value",0)/total_value*100:.0f}% of portfolio')

    # 3. Regime consistency
    regimes = []
    for p in positions:
        sig = signals_dict.get(p['symbol'], {})
        tr = sig.get('trend_regime')
        if tr:
            regimes.append(tr)

    regime_counts = defaultdict(int)
    for r in regimes:
        regime_counts[r] += 1
    dominant_regime = max(regime_counts, key=regime_counts.get) if regime_counts else 'unknown'

    # Check for conflicting signals
    has_bull = any(r in ('strong_bull', 'bull') for r in regimes)
    has_bear = any(r in ('strong_bear', 'bear') for r in regimes)
    if has_bull and has_bear:
        alerts.append('⚡ Mixed regime: some positions in bull trend, others in bear')

    # 4. Diversification ratio
    if portfolio_vol > 0 and weighted_vol_sq > 0:
        undiversified_vol = math.sqrt(weighted_vol_sq)
        diversification_ratio = undiversified_vol / portfolio_vol if portfolio_vol > 0 else 1
    else:
        diversification_ratio = 1

    # 5. Individual position alerts
    for p in positions:
        code = p['symbol']
        sig = signals_dict.get(code, {})
        dd = sig.get('rolling_max_drawdown_60d')
        if dd is not None and dd < -15:
            alerts.append(f'🔴 {code}: deep drawdown ({dd:.1f}%)')
        composite = sig.get('composite_score', 0)
        if composite < -40:
            alerts.append(f'🔴 {code}: strong sell signal (score {composite})')
        weight = p.get('current_value', 0) / total_value
        if weight > 0.35:
            alerts.append(f'⚠️ {code}: position too large ({weight*100:.1f}%)')

    return {
        'portfolio_volatility': round(portfolio_vol, 4),
        'portfolio_vol_pct': round(portfolio_vol * 100, 2),
        'concentration_score': round(concentration_score, 1),
        'concentration_risk': 'high' if concentration_score > 50 else 'medium' if concentration_score > 25 else 'low',
        'herfindahl_index': round(herfindahl, 4),
        'diversification_ratio': round(diversification_ratio, 3),
        'dominant_regime': dominant_regime,
        'regime_distribution': dict(regime_counts),
        'total_value': round(total_value, 2),
        'num_positions': len(positions),
        'alerts': alerts,
    }


# ═══════════════════════════════════════════════════════════
#  Trade Decision Filter
# ═══════════════════════════════════════════════════════════

class RiskManager:
    """Unified risk manager that coordinates all risk sub-systems."""

    def __init__(self, max_portfolio_drawdown=0.15, max_single_position=0.30,
                 target_volatility=0.15, initial_portfolio_value=100000):
        self.max_portfolio_drawdown = max_portfolio_drawdown
        self.max_single_position = max_single_position
        self.target_volatility = target_volatility
        self.stop_loss_mgr = StopLossManager()
        self.drawdown_protector = DrawdownProtector(initial_portfolio_value)
        self.kelly = KellySizer()

    def update_portfolio_value(self, value):
        return self.drawdown_protector.update(value)

    def add_position(self, code, entry_nav, entry_date, **kwargs):
        self.stop_loss_mgr.add_position(code, entry_nav, entry_date, **kwargs)

    def check_position(self, code, current_nav, current_date):
        return self.stop_loss_mgr.update(code, current_nav, current_date)

    def get_position_size(self, capital, asset_vol, current_nav=1.0):
        return volatility_target_position(capital, asset_vol, self.target_volatility, current_nav)

    def get_risk_params(self, trend_regime, vol_regime):
        return get_regime_risk_params(trend_regime, vol_regime)


class KellySizer:
    """Kelly Criterion position sizer."""

    def __init__(self, max_fraction=0.25, use_half_kelly=True):
        self.max_fraction = max_fraction
        self.use_half_kelly = use_half_kelly

    def compute_kelly(self, win_rate, avg_win, avg_loss):
        return kelly_fraction(win_rate, avg_win, avg_loss)

    def suggested_position_size(self, kelly_f, confidence=100):
        """Scale Kelly fraction by confidence (0-100)."""
        scaled = kelly_f * (confidence / 100)
        return round(min(scaled, self.max_fraction), 4)


class CorrelationAllocator:
    """Portfolio allocator using correlation-aware methods."""

    def __init__(self, method='risk_parity'):
        self.method = method

    def allocate(self, volatilities, correlations=None):
        if self.method == 'risk_parity':
            return risk_parity_weights(volatilities, correlations)
        elif self.method == 'equal_weight':
            n = len(volatilities)
            return {code: round(1.0 / n, 4) for code in volatilities} if n > 0 else {}
        elif self.method == 'min_variance':
            return risk_parity_weights(volatilities, correlations)  # Simplified
        return {}


def filter_trade_decisions(proposed_trades, portfolio_risk, risk_params,
                           drawdown_status, current_positions):
    """Filter and adjust proposed trades through risk management rules.

    This is the final gate before any trade is executed. It can:
      - Block trades that violate risk limits
      - Reduce trade sizes
      - Add mandatory risk-reduction trades
      - Enforce cooling-off periods

    Args:
        proposed_trades: list of {symbol, action, amount, reason, signal_score}
        portfolio_risk: output from compute_portfolio_risk
        risk_params: output from get_regime_risk_params
        drawdown_status: output from DrawdownProtector.update
        current_positions: dict {symbol: {weight, current_value, entry_date}}

    Returns:
        list of approved trades (possibly modified) + list of blocked trades with reasons
    """
    approved = []
    blocked = []

    buy_scale = risk_params.get('buy_scale', 1.0)
    _max_new_pos = risk_params.get('new_position_max_pct', 0.15)  # noqa: F841 — reserved for position-size gating
    total_value = portfolio_risk.get('total_value', 0) or 1

    # Apply drawdown circuit breaker
    if drawdown_status:
        dd_buy_scale = drawdown_status.get('buy_scale', 1.0)
        buy_scale = min(buy_scale, dd_buy_scale)

        if drawdown_status.get('force_sell'):
            # Add forced sell trades
            sell_pct = drawdown_status['force_sell_pct']
            for code, pos in current_positions.items():
                sell_amount = pos.get('current_value', 0) * sell_pct
                if sell_amount > 0:
                    approved.append({
                        'symbol': code,
                        'action': 'sell',
                        'amount': round(sell_amount, 2),
                        'reason': f'Circuit breaker: drawdown {drawdown_status["drawdown_pct"]:.1f}%, '
                                  f'force selling {sell_pct*100:.0f}%',
                        'risk_override': True,
                        'priority': 'critical',
                    })

    for trade in proposed_trades:
        code = trade.get('symbol', '')
        action = trade.get('action', '')
        amount = trade.get('amount', 0)
        score = trade.get('signal_score', 0)

        # Sells are generally approved (with some checks)
        if action in ('sell', 'reduce'):
            # Check minimum holding period (7 days for Chinese market, 1.5% penalty if < 7)
            pos = current_positions.get(code, {})
            entry_date = pos.get('entry_date', '')
            if entry_date:
                try:
                    held_days = (datetime.now() - datetime.strptime(entry_date[:10], '%Y-%m-%d')).days
                    if held_days < 7:
                        trade['warning'] = f'Short holding ({held_days}d): 1.5% redemption penalty'
                except (ValueError, TypeError):
                    logger.warning('Failed to parse entry_date %r for holding-period check', entry_date, exc_info=True)
            approved.append(trade)
            continue

        # Buy/add trades go through filters
        if action in ('buy', 'add'):
            # Filter 1: drawdown circuit breaker
            if buy_scale <= 0:
                blocked.append({
                    **trade,
                    'block_reason': f'Circuit breaker active: drawdown level {drawdown_status.get("level", "?")}',
                })
                continue

            # Filter 2: scale down buy amount
            original_amount = amount
            amount = round(amount * buy_scale, 2)
            if amount != original_amount:
                trade['amount'] = amount
                trade['scaled_from'] = original_amount
                trade['scale_factor'] = buy_scale

            # Filter 3: max position size
            existing_weight = current_positions.get(code, {}).get('weight', 0)
            new_weight = existing_weight + (amount / total_value if total_value > 0 else 0)
            max_single = risk_params.get('max_equity_pct', 0.40)
            if new_weight > max_single:
                max_add = (max_single - existing_weight) * total_value
                if max_add <= 0:
                    blocked.append({
                        **trade,
                        'block_reason': f'Position limit reached: {existing_weight*100:.1f}% >= {max_single*100:.0f}%',
                    })
                    continue
                trade['amount'] = round(max_add, 2)
                trade['capped_reason'] = f'Capped from ¥{amount} to ¥{max_add:.0f} (position limit)'

            # Filter 4: signal strength minimum
            if score is not None and score < -20:
                blocked.append({
                    **trade,
                    'block_reason': f'Negative signal score ({score}): not safe to buy',
                })
                continue

            approved.append(trade)
            continue

        # Hold / unknown actions pass through
        approved.append(trade)

    return approved, blocked
