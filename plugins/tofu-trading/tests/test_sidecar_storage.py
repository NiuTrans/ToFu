"""Executable contracts for the tofu-trading sidecar repository."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from lib.storage import StorageSupervisor
from tofu_trading.storage import (
    TradingConnection,
    TradingDocumentRepository,
    migrate_legacy_storage,
)
from tofu_trading.storage_schema import TABLE_DDL
from tofu_trading.transactions import write_transaction


pytestmark = pytest.mark.unit


def test_critical_lifecycle_loggers_reach_host_business_log():
    from tofu_trading import storage
    from tofu_trading import web

    assert storage.logger.name.startswith("lib.")
    assert web.logger.name.startswith("routes.")


@pytest.fixture
def migrated_storage(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "tofu.db"
    connection = sqlite3.connect(database_path)
    connection.execute(TABLE_DDL["trading_holdings"])
    connection.execute(TABLE_DDL["trading_price_cache"])
    connection.executemany(
        "INSERT INTO trading_holdings "
        "(user_id, symbol, shares, buy_price) VALUES (?, ?, ?, ?)",
        [(1, "510300", 10, 4.1), (2, "000001", 20, 11.2)],
    )
    connection.execute(
        "INSERT INTO trading_price_cache "
        "(symbol, asset_name, nav) VALUES (?, ?, ?)",
        ("510300", "沪深300ETF", 4.2),
    )
    connection.commit()
    connection.close()

    supervisor = StorageSupervisor(
        project_root=tmp_path, backend="sqlite", startup_timeout=60
    )
    supervisor.start()
    repository = TradingDocumentRepository(
        client_factory=lambda write=False: supervisor.client
    )
    repository.register()
    migration = migrate_legacy_storage(repository)
    try:
        yield repository, migration, database_path
    finally:
        supervisor.stop()


def test_legacy_import_is_verified_idempotent_and_non_destructive(
    migrated_storage,
):
    repository, migration, database_path = migrated_storage
    assert migration["total_rows"] == 3
    assert migration["tables"]["trading_holdings"]["rows"] == 2
    assert migrate_legacy_storage(repository)["total_rows"] == 3

    source = sqlite3.connect(database_path)
    try:
        assert source.execute(
            "SELECT COUNT(*) FROM trading_holdings"
        ).fetchone()[0] == 2
    finally:
        source.close()


def test_repository_keys_make_owner_isolation_structural(migrated_storage):
    repository, _, _ = migrated_storage
    owner_one = TradingConnection(1, repository=repository, prepare=False)
    owner_two = TradingConnection(2, repository=repository, prepare=False)
    try:
        one = owner_one.execute(
            "SELECT symbol FROM trading_holdings"
        ).fetchall()
        two = owner_two.execute(
            "SELECT symbol FROM trading_holdings"
        ).fetchall()
        assert [row["symbol"] for row in one] == ["510300"]
        assert [row["symbol"] for row in two] == ["000001"]
    finally:
        owner_one.close()
        owner_two.close()


def test_cross_table_write_transaction_commits_as_one_batch(migrated_storage):
    repository, _, _ = migrated_storage
    connection = TradingConnection(1, repository=repository, prepare=False)
    try:
        with write_transaction(connection, label="portfolio-adjustment"):
            connection.execute(
                "UPDATE trading_holdings SET shares=? "
                "WHERE symbol=? AND user_id=?",
                (12, "510300", 1),
            )
            connection.execute(
                "INSERT INTO trading_transactions "
                "(user_id, symbol, shares, price, amount) "
                "VALUES (?, ?, ?, ?, ?)",
                (1, "510300", 2, 4.2, 8.4),
            )
    finally:
        connection.close()

    reloaded = TradingConnection(1, repository=repository, prepare=False)
    try:
        assert reloaded.execute(
            "SELECT shares FROM trading_holdings WHERE symbol=?", ("510300",)
        ).fetchone()["shares"] == 12
        assert reloaded.execute(
            "SELECT COUNT(*) AS count FROM trading_transactions"
        ).fetchone()["count"] == 1
    finally:
        reloaded.close()


def test_cross_owner_write_is_rejected_before_sidecar_commit(migrated_storage):
    repository, _, _ = migrated_storage
    connection = TradingConnection(1, repository=repository, prepare=False)
    try:
        connection.execute(
            "INSERT INTO trading_holdings (user_id, symbol) VALUES (?, ?)",
            (2, "forbidden"),
        )
        with pytest.raises(RuntimeError, match="cross-owner write denied"):
            connection.commit()
    finally:
        connection.close()


def test_simulator_schema_preserves_runtime_defaults(migrated_storage):
    repository, _, _ = migrated_storage
    connection = TradingConnection(1, repository=repository, prepare=False)
    try:
        connection.execute(
            "INSERT INTO trading_sim_prices "
            "(symbol, date, nav, open, close) VALUES (?, ?, ?, ?, ?)",
            ("new-symbol", "2026-08-26", 1.2, 1.1, 1.2),
        )
        connection.commit()
    finally:
        connection.close()

    reloaded = TradingConnection(1, repository=repository, prepare=False)
    try:
        row = reloaded.execute(
            "SELECT acc_nav, change_pct, volume, amount "
            "FROM trading_sim_prices WHERE symbol=?",
            ("new-symbol",),
        ).fetchone()
        assert dict(row) == {
            "acc_nav": 0,
            "change_pct": 0,
            "volume": 0,
            "amount": 0,
        }
    finally:
        reloaded.close()


def test_failed_statement_does_not_discard_prior_pending_write(migrated_storage):
    repository, _, _ = migrated_storage
    connection = TradingConnection(1, repository=repository, prepare=False)
    try:
        connection.execute(
            "INSERT INTO trading_holdings (user_id, symbol, shares) "
            "VALUES (?, ?, ?)",
            (1, "preserved", 3),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO trading_holdings (user_id, symbol) VALUES (?, ?)",
                (1, None),
            )
        connection.commit()
    finally:
        connection.close()

    reloaded = TradingConnection(1, repository=repository, prepare=False)
    try:
        assert reloaded.execute(
            "SELECT shares FROM trading_holdings WHERE symbol='preserved'"
        ).fetchone()["shares"] == 3
    finally:
        reloaded.close()


def test_connection_context_commits_implicit_transaction(migrated_storage):
    repository, _, _ = migrated_storage
    with TradingConnection(1, repository=repository, prepare=False) as connection:
        connection.execute(
            "INSERT INTO trading_holdings (user_id, symbol, shares) "
            "VALUES (?, ?, ?)",
            (1, "context-commit", 7),
        )

    reloaded = TradingConnection(1, repository=repository, prepare=False)
    try:
        assert reloaded.execute(
            "SELECT shares FROM trading_holdings WHERE symbol='context-commit'"
        ).fetchone()["shares"] == 7
    finally:
        reloaded.close()
