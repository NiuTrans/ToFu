"""Stage simulation records, then settle through the owner-bound transaction seam.

SimulationSettlement owns a single phase's pending writes and committed events.
Model/data work happens before commit; transactions.py and storage.py remain the
only transaction and durable repository authorities. No batch is retried here.
"""
from __future__ import annotations

from datetime import datetime
import json

from ..transactions import write_transaction


class SimulationSettlement:
    """One-use batch of required records and events describing those records."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._writes: list[tuple[str, tuple]] = []
        self._events: list[tuple[str, dict]] = []

    def stage(self, sql: str, params: tuple) -> None:
        self._writes.append((sql, params))

    def emit_after_commit(self, kind: str, data: dict) -> None:
        self._events.append((kind, data))

    def commit(self, db, emit) -> None:
        writes, events = self._writes, self._events
        self._writes, self._events = [], []
        with write_transaction(db, label='simulation settlement'):
            for sql, params in writes:
                db.execute(sql, params)
        for kind, data in events:
            emit(kind, data)

    def record_strategy_performance(self, strategies_used: list[dict],
                                    trade_return_pct: float, trade_info: dict):
        """Record which strategies contributed to a trade outcome.

        Called after each sell (closed trade) to update strategy performance.
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for s in strategies_used:
            sid = s.get('id')
            if not sid:
                continue
            self.stage('''
                INSERT INTO trading_strategy_performance
                (strategy_id, period_start, period_end, return_pct,
                 source, detail_json, created_at)
                VALUES (?, ?, ?, ?, 'sim', ?, ?)
            ''', (
                sid,
                trade_info.get('buy_date', now),
                trade_info.get('sell_date', now),
                trade_return_pct,
                json.dumps({
                    'session_id': self.session_id,
                    'symbol': trade_info.get('symbol', ''),
                    'strategy_name': s.get('name', ''),
                }, ensure_ascii=False),
                now,
            ))

    def add_journal(self, sim_date, entry_type, action, symbol,
                    reasoning, signals=None, confidence=0, amount=0):
        """Add entry to simulation journal."""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.stage('''
            INSERT INTO trading_sim_journal
            (session_id, sim_date, entry_type, action, symbol, amount,
             reasoning, signals_json, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            self.session_id, sim_date, entry_type, action, symbol, amount,
            reasoning[:10000] if reasoning else '',
            json.dumps(signals or {}, ensure_ascii=False, default=str),
            confidence, now,
        ))

    def record_trade(self, sim_date, action, symbol, shares, price, buy_price, pnl_pct, reason):
        """Record a trade in sim_positions table.

        Args:
            sim_date: Trade date.
            action: 'buy' or 'sell'.
            symbol: Fund/ETF code.
            shares: Number of shares.
            price: Trade price (nav).
            buy_price: Original buy price (for PNL calc on sell; same as price for buy).
            pnl_pct: P&L percentage (0 for buy).
            reason: Trade reason text.
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if action == 'buy':
            self.stage('''
                INSERT INTO trading_sim_positions
                (session_id, symbol, shares, buy_price, buy_date,
                 current_price, status, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
            ''', (self.session_id, symbol, shares, price, sim_date, price, reason, now))
        elif action == 'sell':
            pnl_abs = (price - buy_price) * shares
            self.stage('''
                UPDATE trading_sim_positions SET
                  status='closed', close_price=?, close_date=?,
                  pnl=?, pnl_pct=?
                WHERE session_id=? AND symbol=? AND status='open'
            ''', (price, sim_date, pnl_abs, pnl_pct,
                  self.session_id, symbol))


def create_simulation_session(db, session_id, config, decision_dates) -> None:
    """Publish the running session before any execution/progress events."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with write_transaction(db, label='simulation start'):
        db.execute('''
            INSERT INTO trading_sim_sessions
            (session_id, status, initial_capital, current_cash, symbols,
             start_date, end_date, step_days, current_sim_date,
             total_steps, completed_steps, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            session_id, 'running', config.initial_capital, config.initial_capital,
            json.dumps(config.symbols), config.start_date, config.end_date,
            config.step_days, config.start_date, len(decision_dates), 0,
            json.dumps(config.to_dict(), ensure_ascii=False), now, now,
        ))


def mark_simulation_failed(db, session_id: str) -> None:
    """Mark a failed running session; the run owner preserves its original error.

    A lost acknowledgement may mean the batch committed. The storage connection
    reloads authoritative rows after commit failure; there is no execution retry.
    If rollback fails, do not write a marker over uncertain pending mutations.
    """
    db.rollback()
    with write_transaction(db, label='simulation failure'):
        db.execute("UPDATE trading_sim_sessions SET status='failed', updated_at=? "
                   "WHERE session_id=? AND status='running'",
                   (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), session_id))
