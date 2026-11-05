"""Apply model trade intentions through deterministic simulation policy.

Entry: SimulationDecisionExecution.apply. This step-local owner uses the live
configuration/ledger and supplied market ports. It stages writes and events in
the existing SimulationSettlement; the coordinator owns commit and rollback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from logging import Logger

    from ..storage import TradingConnection
    from .llm_simulator import SimulatorConfig
    from .simulation_ledger import SimulationLedger
    from .simulation_persistence import SimulationSettlement


SimulationEmitter = Callable[[str, dict], None]


@dataclass(frozen=True)
class SimulationExecutionPorts:
    """Market and diagnostic dependencies supplied by the simulation facade."""

    get_price_at: Callable[[TradingConnection, str, str], dict | None]
    validate_symbol_code: Callable[[str], bool]
    ensure_price_data: Callable[
        [TradingConnection, str, str, str, SimulationEmitter, int], bool
    ]
    resolve_symbol_name: Callable[[str], str]
    next_bar_fill: Callable[
        [TradingConnection, str, str, str], tuple[float | None, str | None]
    ]
    holding_days: Callable[[str, str], int]
    logger: Logger


@dataclass(frozen=True)
class SimulationDecisionExecution:
    """One step's admitted decisions, sharing the coordinator's mutable state."""

    db: TradingConnection
    config: SimulatorConfig
    ledger: SimulationLedger
    positions: dict[str, dict]
    trade_log: list[dict]
    settlement: SimulationSettlement
    sim_date: str
    step_index: int
    portfolio_value: float
    strategies_used: list[dict]
    symbol_names: dict[str, str]
    emit: SimulationEmitter
    ports: SimulationExecutionPorts

    def apply(self, decisions: list[dict]) -> None:
        """Keep intent order, including holds, rejected trades and partial errors."""
        for dec in decisions:
            action = dec.get("action", "hold")
            symbol = dec.get("symbol", "")
            confidence = dec.get("confidence", 0)
            reason = dec.get("reason", "")

            if action == "hold":
                self.settlement.add_journal(
                    self.sim_date, "decision", "hold", symbol, reason, dec, confidence
                )
                continue

            if confidence < self.config.min_confidence:
                self.settlement.add_journal(
                    self.sim_date,
                    "skip",
                    action,
                    symbol,
                    f"信心度{confidence}<{self.config.min_confidence}，跳过",
                    dec,
                    confidence,
                )
                continue

            if action == "buy" and symbol:
                self._buy(dec, symbol, confidence, reason)
            elif action == "sell" and symbol:
                self._sell(dec, symbol, confidence, reason)

    def _buy(self, dec: dict, symbol: str, confidence: float, reason: str) -> None:
        if symbol in self.positions:
            self.ports.logger.info(
                "[Sim] Skip buy %s at %s: already holding", symbol, self.sim_date
            )
            self.settlement.add_journal(
                self.sim_date,
                "skip",
                "buy",
                symbol,
                "已持有该标的，跳过重复买入",
                dec,
                confidence,
            )
            return
        if len(self.positions) >= self.config.max_positions:
            self.ports.logger.info(
                "[Sim] Skip buy %s at %s: position limit (%d/%d)",
                symbol,
                self.sim_date,
                len(self.positions),
                self.config.max_positions,
            )
            self.settlement.add_journal(
                self.sim_date,
                "skip",
                "buy",
                symbol,
                f"持仓数已达上限({len(self.positions)}/{self.config.max_positions})",
                dec,
                confidence,
            )
            return

        amount = float(dec.get("amount", 0))
        if amount <= 0:
            self.ports.logger.info(
                "[Sim] Skip buy %s at %s: amount<=0", symbol, self.sim_date
            )
            self.settlement.add_journal(
                self.sim_date, "skip", "buy", symbol, "买入金额<=0", dec, confidence
            )
            return
        # Position cap follows CURRENT equity, not initial capital.
        # Anchored to initial_capital, a portfolio down 50% kept the
        # same absolute cap — i.e. it silently doubled leverage per
        # trade exactly when it could least afford it.
        max_amount = self.portfolio_value * self.config.max_position_pct / 100
        amount = min(amount, self.ledger.cash * 0.95, max_amount)
        if amount < 100:
            self.ports.logger.info(
                "[Sim] Skip buy %s at %s: amount=%s < 100 after capping (cash=%.0f)",
                symbol,
                self.sim_date,
                amount,
                self.ledger.cash,
            )
            self.settlement.add_journal(
                self.sim_date,
                "skip",
                "buy",
                symbol,
                f"可用金额不足(cash={self.ledger.cash:.0f}，计算后amount={amount:.0f}<100)",
                dec,
                confidence,
            )
            return

        price_data = self.ports.get_price_at(self.db, symbol, self.sim_date)
        if not price_data or price_data["nav"] <= 0:
            # ── Open-universe: on-demand price fetch for new symbols ──
            if (
                self.ports.validate_symbol_code(symbol)
                and symbol not in self.config.symbols
            ):
                self.ports.logger.info(
                    "[Sim] Symbol %s not in seed list, attempting on-demand fetch",
                    symbol,
                )
                fetched_ok = self.ports.ensure_price_data(
                    self.db,
                    symbol,
                    self.config.start_date,
                    self.config.end_date,
                    self.emit,
                    self.step_index,
                )
                if fetched_ok:
                    price_data = self.ports.get_price_at(self.db, symbol, self.sim_date)
                    # Also resolve the name
                    self.ports.resolve_symbol_name(symbol)

            if not price_data or price_data["nav"] <= 0:
                self.settlement.add_journal(
                    self.sim_date,
                    "skip",
                    "buy",
                    symbol,
                    "无价格数据（代码可能无效或该日期无交易）",
                    dec,
                    confidence,
                )
                return

        # Execution price: T+1 OPEN, never T's close (see
        # _next_bar_fill — deciding on T's close and filling at that
        # same close is a look-ahead no participant can realise).
        nav, _fill_err = self.ports.next_bar_fill(
            self.db, symbol, self.sim_date, self.config.end_date
        )
        if nav is None:
            self.ports.logger.info(
                "[Sim] Skip buy %s decided %s: %s", symbol, self.sim_date, _fill_err
            )
            self.settlement.add_journal(
                self.sim_date, "skip", "buy", symbol, _fill_err, dec, confidence
            )
            return

        shares = self.ledger.buy(symbol, amount, nav, fee_book=self.config.fee_book)

        # Item 4 & 6: Track dynamically-added symbols in session
        _sym_name = self.symbol_names.get(symbol, symbol)
        if symbol not in self.config.symbols:
            # Dynamically discovered — add to tracked symbols
            self.config.symbols.append(symbol)
            if _sym_name == symbol:
                _sym_name = self.ports.resolve_symbol_name(symbol)
            self.ports.logger.info(
                "[Sim] Added dynamic symbol %s (%s) to tracked list", symbol, _sym_name
            )

        self.positions[symbol] = {
            "shares": shares,
            "buy_price": nav,
            "buy_date": self.sim_date,
            "buy_step": self.step_index,
            "current_price": nav,
            "name": _sym_name,
            "strategies_used": self.strategies_used,  # track for perf recording
        }

        self.settlement.add_journal(
            self.sim_date,
            "decision",
            "buy",
            symbol,
            reason,
            {"amount": amount, "price": nav, "shares": shares},
            confidence,
            amount,
        )

        self.settlement.record_trade(
            self.sim_date, "buy", symbol, shares, nav, nav, 0, reason
        )
        self.trade_log.append(
            {
                "date": self.sim_date,
                "action": "buy",
                "symbol": symbol,
                "price": nav,
                "amount": amount,
            }
        )

        self.settlement.emit_after_commit(
            "sim_trade",
            {
                "step": self.step_index + 1,
                "sim_date": self.sim_date,
                "action": "buy",
                "symbol": symbol,
                "symbol_name": self.symbol_names.get(symbol, symbol),
                "amount": amount,
                "price": nav,
            },
        )

    def _sell(self, dec: dict, symbol: str, confidence: float, reason: str) -> None:
        if symbol not in self.positions:
            self.ports.logger.info(
                "[Sim] Skip sell %s at %s: not in current positions",
                symbol,
                self.sim_date,
            )
            self.settlement.add_journal(
                self.sim_date,
                "skip",
                "sell",
                symbol,
                "当前未持有该标的，无法卖出",
                dec,
                confidence,
            )
            return

        pos = self.positions[symbol]

        # T+1 check
        if self.config.t_plus_1 and pos["buy_date"] == self.sim_date:
            self.settlement.add_journal(
                self.sim_date,
                "skip",
                "sell",
                symbol,
                "T+1限制：买入当日不能卖出",
                dec,
                confidence,
            )
            return

        # Same rule as the buy side: a decision made on T fills at
        # T+1's open. No fallback to T's close.
        close_price, _fill_err = self.ports.next_bar_fill(
            self.db, symbol, self.sim_date, self.config.end_date
        )
        if close_price is None:
            self.ports.logger.info(
                "[Sim] Skip sell %s decided %s: %s", symbol, self.sim_date, _fill_err
            )
            self.settlement.add_journal(
                self.sim_date, "skip", "sell", symbol, _fill_err, dec, confidence
            )
            return
        pnl_pct = self.ledger.sell(
            symbol,
            pos,
            close_price,
            fee_book=self.config.fee_book,
            holding_days=self.ports.holding_days(pos["buy_date"], self.sim_date),
        )

        # Record per-strategy performance (feedback loop)
        buy_strategies = pos.get("strategies_used", [])
        sell_strategies = self.strategies_used
        # Credit both buy-side and sell-side strategies
        all_strats = {s.get("name", ""): s for s in buy_strategies + sell_strategies}
        self.settlement.record_strategy_performance(
            list(all_strats.values()),
            pnl_pct,
            {"symbol": symbol, "buy_date": pos["buy_date"], "sell_date": self.sim_date},
        )

        self.settlement.add_journal(
            self.sim_date,
            "decision",
            "sell",
            symbol,
            reason,
            {
                "price": close_price,
                "pnl_pct": pnl_pct,
                "strategies_used": [s.get("name", "") for s in all_strats.values()],
            },
            confidence,
        )

        self.settlement.record_trade(
            self.sim_date,
            "sell",
            symbol,
            pos["shares"],
            close_price,
            pos["buy_price"],
            pnl_pct,
            reason,
        )
        self.trade_log.append(
            {
                "date": self.sim_date,
                "action": "sell",
                "symbol": symbol,
                "price": close_price,
                "pnl_pct": pnl_pct,
            }
        )

        del self.positions[symbol]

        self.settlement.emit_after_commit(
            "sim_trade",
            {
                "step": self.step_index + 1,
                "sim_date": self.sim_date,
                "action": "sell",
                "symbol": symbol,
                "symbol_name": self.symbol_names.get(symbol, symbol),
                "price": close_price,
                "pnl_pct": pnl_pct,
            },
        )
