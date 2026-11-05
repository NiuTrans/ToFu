"""Cash, charged fees and close outcomes for one historical simulation run.

Entry: SimulationLedger. FeeBook owns fee policy; the engine supplies admitted
fills and owns positions, persistence, risk decisions and event ordering.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .fee_book import FeeBook


class SimulationLedger:
    """Run-local accounting using the same fee book exposed in model context."""

    def __init__(self, initial_capital: float):
        self.cash = initial_capital
        self.total_fees = 0.0
        self.total_trades = 0
        self.winning_trades = 0

    def buy(self, symbol: str, amount: float, price: float, *, fee_book: FeeBook) -> float:
        """Debit an admitted purchase and return shares after the charged fee."""
        fee = fee_book.fee_for(symbol, amount, 'buy')['fee_amount']
        shares = (amount - fee) / price
        self.cash -= amount
        self.total_fees += fee
        return shares

    def sell(self, symbol: str, position: dict, price: float, *,
             fee_book: FeeBook, holding_days: int) -> float:
        """Credit a filled close and return its gross percentage price change."""
        value = price * position['shares']
        fee = fee_book.fee_for(
            symbol, value, 'sell', holding_days=holding_days)['fee_amount']
        self.cash += value - fee
        self.total_fees += fee
        self.total_trades += 1
        pnl_pct = (price - position['buy_price']) / position['buy_price'] * 100
        if pnl_pct > 0:
            self.winning_trades += 1
        return pnl_pct

    def value(self, positions: dict[str, dict]) -> float:
        """Mark held positions at their latest price, falling back to entry."""
        return self.cash + sum(
            position.get('current_price', position['buy_price']) * position['shares']
            for position in positions.values())
