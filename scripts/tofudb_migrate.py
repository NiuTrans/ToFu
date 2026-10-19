"""Explicit resumable migration/rollback gate: legacy SQLite → Tofu-DB.

``docs/STORAGE.md`` lists "explicit resumable migration/rollback" as a hard
release gate before Tofu-DB may be selected as a storage backend.  This tool
is that gate for the first stable domain set:

* reads the legacy SQLite authority through a bounded, driver-level
  read-only connection (``lib.storage_authority_sql.offline`` — the same WAL
  query-only mode ``debug/inspect_conversation.py`` uses) and never writes
  to it;
* replays domain state into a running ``tofu-db serve`` daemon through the
  production storage.v2 wire protocol (``scripts/tofudb_v2_client.py``), so
  migration exercises the same semantic operations production will use;
* checkpoints per-domain completion plus the source digest in a durable
  cursor file, so an interrupted migration resumes and converges — every
  replayed document converges idempotently (credential upsert/update,
  billing natural-key dedupe, record version-CAS ladders);
* verifies per-domain exact counts and content digests between legacy reads
  and Tofu-DB re-reads and emits a machine-readable JSON report;
* never mutates the legacy authority and treats the target directory as
  disposable: rollback is "keep serving legacy, delete the target", and
  cutover remains an explicit, separate operator action (out of scope).

Domain coverage is registry-driven (``DOMAIN_REGISTRY``): each entry owns a
legacy scan, a per-document replay plan, and legacy/target digest builders.
Remaining domains slot in as their operations stabilize; per-domain order
respects inter-domain ID references through ``depends_on``.

Bounded-fault hook (test-only): ``TOFUDB_MIGRATE_FAULT_AFTER_OPS=N`` aborts
the process with exit code 3 after N successfully replayed operations, one
deterministic injection point used by the resume-convergence test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.storage_authority_sql.offline import (  # noqa: E402
    open_readonly_sqlite_authority,
    resolve_readonly_sqlite_authority,
)
from tofu_db.wire import (  # noqa: E402
    StorageV2Error,
    StorageV2Session,
)

# ── Hard bounds (docs/STORAGE.md release-gate constraints) ──────────────
PAGE_ROWS = 1_000
PAGE_BYTES = 8 * 1024 * 1024
CHECKPOINT_VERSION = 1
FAULT_ENV = "TOFUDB_MIGRATE_FAULT_AFTER_OPS"
FAULT_EXIT_CODE = 3
OP_DEADLINE_MS = 30_000

# Wall-clock fields the target engine stamps from its own clock on replay;
# they are excluded from cross-authority digests (presence/type only).
RECORD_DROP_FIELDS = frozenset({"updated_at_ms"})
WALLET_DROP_FIELDS = frozenset({"updated_at"})


class MigrationError(RuntimeError):
    """One fail-closed migration/verification failure."""


# ── Canonical digests ────────────────────────────────────────────────────
def _drop_fields(value: Any, fields: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _drop_fields(item, fields)
            for key, item in sorted(value.items())
            if key not in fields
        }
    if isinstance(value, list):
        return [_drop_fields(item, fields) for item in value]
    return value


def digest_documents(documents: Iterable[Any]) -> str:
    """SHA-256 over length-prefixed canonical JSON of each document."""
    digest = hashlib.sha256()
    for document in documents:
        encoded = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


# ── Bounded legacy scan ──────────────────────────────────────────────────
def _paged_rows(
    connection: Any,
    table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
) -> Iterator[dict[str, Any]]:
    """Keyset-paginated read-only scan, ≤PAGE_ROWS rows and ≤PAGE_BYTES/page."""
    if not 1 <= len(key_columns) <= 2:
        raise MigrationError(f"{table}: unsupported keyset arity")
    select = ", ".join(columns)
    order = ", ".join(key_columns)
    last: tuple[Any, ...] | None = None
    while True:
        if last is None:
            sql = f"SELECT {select} FROM {table} ORDER BY {order} LIMIT ?"
            params: tuple[Any, ...] = (PAGE_ROWS,)
        elif len(key_columns) == 1:
            sql = (
                f"SELECT {select} FROM {table} WHERE {key_columns[0]} > ? "
                f"ORDER BY {order} LIMIT ?"
            )
            params = (*last, PAGE_ROWS)
        else:
            first, second = key_columns
            sql = (
                f"SELECT {select} FROM {table} "
                f"WHERE {first} > ? OR ({first} = ? AND {second} > ?) "
                f"ORDER BY {order} LIMIT ?"
            )
            params = (last[0], last[0], last[1], PAGE_ROWS)
        rows = connection.execute(sql, params).fetchall()
        if not rows:
            return
        page_bytes = 0
        for row in rows:
            document = {column: row[column] for column in columns}
            page_bytes += sum(
                len(value) if isinstance(value, (bytes, str)) else 8
                for value in document.values()
            )
        if page_bytes > PAGE_BYTES:
            raise MigrationError(
                f"{table}: page exceeds {PAGE_BYTES} bytes (bounded scan)")
        for row in rows:
            yield {column: row[column] for column in columns}
        if len(rows) < PAGE_ROWS:
            return
        last = tuple(rows[-1][column] for column in key_columns)


def _load_json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8")
    return json.loads(value)


# ── Replay context ───────────────────────────────────────────────────────
class ReplayContext:
    """One storage.v2 session plus the bounded fault-injection counter."""

    def __init__(self, session: StorageV2Session, fault_after_ops: int | None):
        self._session = session
        self._fault_after_ops = fault_after_ops
        self.replayed_ops = 0

    def command(self, operation: str, payload: Mapping[str, Any],
                command_id: str) -> Any:
        return self._call(operation, payload, command_id=command_id)

    def query(self, operation: str, payload: Mapping[str, Any]) -> Any:
        return self._call(operation, payload, command_id=None)

    def _call(self, operation: str, payload: Mapping[str, Any],
              command_id: str | None) -> Any:
        response = self._session.request(
            operation, dict(payload), command_id=command_id,
            deadline_unix_ms=int(time.time() * 1000) + OP_DEADLINE_MS,
        )
        response.raise_for_error()
        self.replayed_ops += 1
        if (
            self._fault_after_ops is not None
            and self.replayed_ops >= self._fault_after_ops
        ):
            # Bounded deterministic fault hook: simulated mid-migration kill.
            print(
                f"tofudb-migrate: injected fault after "
                f"{self.replayed_ops} operations",
                file=sys.stderr, flush=True,
            )
            os._exit(FAULT_EXIT_CODE)
        return response.value()


def _conflict(error: StorageV2Error) -> bool:
    # Only an identity duplicate is a converged resume. Other conflicts —
    # notably the target's bounded admission quotas — must surface loudly,
    # never be mistaken for an already-migrated row.
    return (
        error.code == "database_conflict"
        and "already exists" in str(error).lower()
    )


# ── Domain: credential (auth_credentials) ────────────────────────────────
_CREDENTIAL_COLUMNS = (
    "id", "owner_user_id", "account_user_id", "tenant_id", "name", "prefix",
    "secret_hash", "scopes", "rate_limit_rpm", "rate_limit_tpd", "created_at",
    "last_used_at", "expires_at", "disabled", "revoked_at", "metadata",
)


def _credential_legacy_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_user_id": int(row["owner_user_id"]),
        "account_user_id": row["account_user_id"] or "",
        "tenant_id": row["tenant_id"] or "",
        "name": row["name"],
        "prefix": row["prefix"],
        "secret_hash": row["secret_hash"],
        "scopes": _load_json(row["scopes"]),
        "rate_limit_rpm": int(row["rate_limit_rpm"] or 0),
        "rate_limit_tpd": int(row["rate_limit_tpd"] or 0),
        "created_at": float(row["created_at"]),
        "last_used_at": (
            None if row["last_used_at"] is None else float(row["last_used_at"])
        ),
        "expires_at": (
            None if row["expires_at"] is None else float(row["expires_at"])
        ),
        "disabled": bool(row["disabled"]),
        "revoked_at": (
            None if row["revoked_at"] is None else float(row["revoked_at"])
        ),
        "metadata": _load_json(row["metadata"]),
    }


def _credential_scan(connection: Any) -> Iterator[dict[str, Any]]:
    for row in _paged_rows(
        connection, "auth_credentials", _CREDENTIAL_COLUMNS, ("id",),
    ):
        yield _credential_legacy_document(row)


def _credential_replay(ctx: ReplayContext, document: dict[str, Any]) -> None:
    """Converge one credential row through the semantic identity operations."""
    boundary = {
        "owner_user_id": document["owner_user_id"],
        "tenant_id": document["tenant_id"],
    }
    credential_id = document["id"]
    base = f"tofudb-mig-credential-{credential_id}"
    create = {
        **boundary,
        "credential_id": credential_id,
        "account_user_id": document["account_user_id"],
        "name": document["name"],
        "prefix": document["prefix"],
        "secret_hash": document["secret_hash"],
        "scopes": document["scopes"],
        "rate_limit_rpm": document["rate_limit_rpm"],
        "rate_limit_tpd": document["rate_limit_tpd"],
        "created_at": document["created_at"],
        "expires_at": document["expires_at"],
        "metadata": document["metadata"],
    }
    try:
        ctx.command("credential.create", create, f"{base}-create")
    except StorageV2Error as error:
        if not _conflict(error):
            raise
        # Resume path: the row already exists; converge the mutable fields.
        updates: dict[str, Any] = {}
        for key in (
            "name", "scopes", "rate_limit_rpm", "rate_limit_tpd",
            "expires_at", "metadata",
        ):
            updates[key] = document[key]
        if not document["revoked_at"]:
            updates["disabled"] = document["disabled"]
        if not document["scopes"]:
            updates.pop("scopes")
        if updates:
            try:
                ctx.command("credential.update", {
                    **boundary, "credential_id": credential_id,
                    "updates": updates,
                }, f"{base}-update")
            except StorageV2Error as update_error:
                if not _conflict(update_error):
                    raise
    # Audit trail: touch only advances; a revoked/disabled tombstone keeps
    # whatever last_used_at it carried (identical on a converged target).
    if document["last_used_at"] is not None and not document["revoked_at"]:
        ctx.command("credential.touch", {
            **boundary, "credential_id": credential_id,
            "used_at": document["last_used_at"],
            "touch_if_before": document["last_used_at"],
        }, f"{base}-touch")
    if document["disabled"] and not document["revoked_at"]:
        ctx.command("credential.update", {
            **boundary, "credential_id": credential_id,
            "updates": {"disabled": True},
        }, f"{base}-disable")
    if document["revoked_at"] is not None:
        ctx.command("credential.revoke", {
            **boundary, "credential_id": credential_id,
            "revoked_at": document["revoked_at"],
        }, f"{base}-revoke")


def _credential_target_document(
    ctx: ReplayContext, document: dict[str, Any],
) -> Any:
    # identify deliberately sees disabled/expired/revoked tombstones and
    # never returns the stored hash, exactly like the legacy projection.
    return ctx.query(
        "credential.identify", {"secret_hash": document["secret_hash"]})


# ── Domain: billing (billing_wallets + billing_ledger) ──────────────────
_LEDGER_COLUMNS = (
    "id", "user_id", "ts", "amount_micro", "kind", "ref_type", "ref_id",
    "balance_after_micro", "note",
)
_WALLET_COLUMNS = (
    "user_id", "balance_micro", "currency", "low_balance_alert_micro",
    "updated_at",
)


def _billing_scan(connection: Any) -> Iterator[dict[str, Any]]:
    """One document per wallet row plus one per ledger entry."""
    for wallet in _paged_rows(
        connection, "billing_wallets", _WALLET_COLUMNS, ("user_id",),
    ):
        yield {
            "kind": "wallet",
            "user_id": wallet["user_id"],
            "balance_micro": int(wallet["balance_micro"]),
            "currency": wallet["currency"],
            "low_balance_alert_micro": int(wallet["low_balance_alert_micro"]),
        }
    for entry in _paged_rows(
        connection, "billing_ledger", _LEDGER_COLUMNS, ("id",),
    ):
        yield {
            "kind": "ledger",
            "id": entry["id"],
            "user_id": entry["user_id"],
            "ts": int(entry["ts"]),
            "amount_micro": int(entry["amount_micro"]),
            "entry_kind": entry["kind"],
            "ref_type": entry["ref_type"] or "",
            "ref_id": entry["ref_id"] or "",
            "balance_after_micro": int(entry["balance_after_micro"]),
            "note": entry["note"] or "",
        }


def _billing_ordered(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    wallets = [doc for doc in documents if doc["kind"] == "wallet"]
    ledger: dict[str, list[dict[str, Any]]] = {}
    for doc in documents:
        if doc["kind"] == "ledger":
            ledger.setdefault(doc["user_id"], []).append(doc)
    for entries in ledger.values():
        entries.sort(key=lambda entry: (entry["ts"], entry["id"]))
    return wallets, ledger


def _billing_replay(ctx: ReplayContext, documents: list[dict[str, Any]]) -> None:
    """Replay ledger history per user in commit order via wallet.apply.

    ``billing.wallet.apply`` is atomic (wallet + ledger entry in one
    transaction) and dedupes on the (kind, ref_type, ref_id) natural key,
    so an interrupted replay converges exactly.
    """
    wallets, ledger = _billing_ordered(documents)
    users = sorted({doc["user_id"] for doc in wallets} | set(ledger))
    for user_id in users:
        for sequence, entry in enumerate(ledger.get(user_id, [])):
            ctx.command("billing.wallet.apply", {
                "user_id": user_id,
                "amount_micro": entry["amount_micro"],
                "kind": entry["entry_kind"],
                "ref_type": entry["ref_type"],
                "ref_id": entry["ref_id"],
                "note": entry["note"],
                "ledger_id": entry["id"],
                "occurred_at": entry["ts"],
                # Legacy data was committed under its own invariant checks;
                # replay must reproduce even an overdrawn history exactly.
                "allow_negative": True,
            }, f"tofudb-mig-billing-{user_id}-{sequence:08d}")


def _billing_target_documents(
    ctx: ReplayContext, documents: list[dict[str, Any]],
) -> Iterator[Any]:
    wallets, _ledger = _billing_ordered(documents)
    for wallet in sorted(wallets, key=lambda doc: doc["user_id"]):
        yield _drop_fields(
            ctx.query("billing.wallet.get", {"user_id": wallet["user_id"]}),
            WALLET_DROP_FIELDS,
        )
    users = sorted({doc["user_id"] for doc in documents})
    for user_id in users:
        entries: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = ctx.query("billing.ledger.list", {
                "user_id": user_id, "limit": PAGE_ROWS, "offset": offset,
                "kinds": [],
            })
            entries.extend(page)
            if len(page) < PAGE_ROWS:
                break
            offset += len(page)
        entries.sort(key=lambda entry: (entry["ts"], entry["id"]))
        yield entries


def _billing_legacy_documents(
    documents: list[dict[str, Any]],
) -> Iterator[Any]:
    wallets, ledger = _billing_ordered(documents)
    for wallet in sorted(wallets, key=lambda doc: doc["user_id"]):
        yield {
            "user_id": wallet["user_id"],
            "balance_micro": wallet["balance_micro"],
            "currency": wallet["currency"],
            "low_balance_alert_micro": wallet["low_balance_alert_micro"],
        }
    for user_id in sorted(ledger):
        yield [
            {
                "id": entry["id"],
                "user_id": entry["user_id"],
                "ts": entry["ts"],
                "amount_micro": entry["amount_micro"],
                "kind": entry["entry_kind"],
                "ref_type": entry["ref_type"],
                "ref_id": entry["ref_id"],
                "balance_after_micro": entry["balance_after_micro"],
                "note": entry["note"],
            }
            for entry in ledger[user_id]
        ]


# ── Domain: record (storage_records) ────────────────────────────────────
_RECORD_COLUMNS = ("namespace", "record_key", "value_json", "version")


def _record_scan(connection: Any) -> Iterator[dict[str, Any]]:
    from lib.storage_authority_sql.operations_pkg._records import _load_record_value

    rows = _paged_rows(
        connection, "storage_records", _RECORD_COLUMNS,
        ("namespace", "record_key"),
    )
    for row in rows:
        namespace = row["namespace"]
        yield {
            "namespace": namespace,
            "key": row["record_key"],
            "value": _load_record_value(namespace, row["value_json"]),
            "version": int(row["version"]),
        }


def _record_replay(ctx: ReplayContext, document: dict[str, Any]) -> None:
    """Version-CAS ladder: converge value and exact version idempotently."""
    namespace = document["namespace"]
    key = document["key"]
    base = f"tofudb-mig-record-{namespace}-{key}"
    current = ctx.query("record.get", {"namespace": namespace, "key": key})
    current_version = 0 if current is None else int(current["version"])
    target_version = document["version"]
    if current_version > target_version:
        raise MigrationError(
            f"record {namespace}/{key}: target version {current_version} "
            f"already exceeds legacy version {target_version}")
    while current_version < target_version:
        last = current_version + 1 == target_version
        result = ctx.command("record.put", {
            "namespace": namespace,
            "key": key,
            "value": document["value"],
            "expected_version": current_version,
        }, f"{base}-{current_version + 1:08d}")
        current_version = int(result["version"])
        if not last and current_version >= target_version:
            raise MigrationError(
                f"record {namespace}/{key}: version ladder overshot")


def _record_target_document(ctx: ReplayContext, document: dict[str, Any]) -> Any:
    return _drop_fields(
        ctx.query("record.get", {
            "namespace": document["namespace"], "key": document["key"],
        }),
        RECORD_DROP_FIELDS,
    )


def _record_legacy_document(document: dict[str, Any]) -> Any:
    # Same projection shape as record.get (value/version; updated_at_ms is a
    # wall-clock field excluded from cross-authority digests).
    return {
        "value": document["value"],
        "version": document["version"],
    }


# ── Domain registry ──────────────────────────────────────────────────────
class DomainSpec:
    """One migratable domain: scan → replay → digest, both authorities."""

    def __init__(
        self,
        name: str,
        *,
        depends_on: Sequence[str],
        scan: Callable[[Any], Iterator[dict[str, Any]]],
        replay: Callable[[ReplayContext, list[dict[str, Any]]], None],
        legacy_projection: Callable[[list[dict[str, Any]]], Iterator[Any]],
        target_projection: Callable[
            [ReplayContext, list[dict[str, Any]]], Iterator[Any]],
    ):
        self.name = name
        self.depends_on = tuple(depends_on)
        self.scan = scan
        self.replay = replay
        self.legacy_projection = legacy_projection
        self.target_projection = target_projection


def _credential_domain() -> DomainSpec:
    return DomainSpec(
        "credential",
        depends_on=(),
        scan=_credential_scan,
        replay=lambda ctx, docs: [
            _credential_replay(ctx, doc) for doc in docs],
        # identify never returns the stored hash; digests compare the
        # public projection only (both sides drop secret_hash).
        legacy_projection=lambda docs: (
            {key: value for key, value in doc.items() if key != "secret_hash"}
            for doc in docs
        ),
        target_projection=lambda ctx, docs: (
            _credential_target_document(ctx, doc) for doc in docs),
    )


def _billing_domain() -> DomainSpec:
    return DomainSpec(
        "billing",
        depends_on=(),
        scan=_billing_scan,
        replay=_billing_replay,
        legacy_projection=_billing_legacy_documents,
        target_projection=_billing_target_documents,
    )


def _record_domain() -> DomainSpec:
    return DomainSpec(
        "record",
        depends_on=(),
        scan=_record_scan,
        replay=lambda ctx, docs: [_record_replay(ctx, doc) for doc in docs],
        legacy_projection=lambda docs: (
            _record_legacy_document(doc) for doc in docs),
        target_projection=lambda ctx, docs: (
            _record_target_document(ctx, doc) for doc in docs),
    )


def domain_registry() -> dict[str, DomainSpec]:
    """Registry of migrated domains in dependency-safe order."""
    specs = [_credential_domain(), _billing_domain(), _record_domain()]
    registry = {spec.name: spec for spec in specs}
    for spec in specs:
        for dependency in spec.depends_on:
            if dependency not in registry:
                raise MigrationError(
                    f"domain {spec.name} depends on unknown {dependency}")
    return registry


def ordered_domains(registry: Mapping[str, DomainSpec]) -> list[DomainSpec]:
    """Toposort so inter-domain ID references land before their consumers."""
    ordered: list[DomainSpec] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            raise MigrationError(f"domain dependency cycle at {name}")
        visiting.add(name)
        for dependency in registry[name].depends_on:
            visit(dependency)
        visiting.discard(name)
        done.add(name)
        ordered.append(registry[name])

    for name in registry:
        visit(name)
    return ordered


# ── Checkpoint cursor ────────────────────────────────────────────────────
class Checkpoint:
    """Durable per-domain resume cursor (atomic rename publication)."""

    def __init__(self, path: Path):
        self._path = path
        self._state: dict[str, Any] = {"version": CHECKPOINT_VERSION, "domains": {}}
        if path.is_file():
            if path.stat().st_size > 1024 * 1024:
                raise MigrationError(f"checkpoint file too large: {path}")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if loaded.get("version") != CHECKPOINT_VERSION:
                raise MigrationError(f"unsupported checkpoint version in {path}")
            self._state = loaded

    def domain(self, name: str) -> dict[str, Any] | None:
        entry = self._state.get("domains", {}).get(name)
        return entry if isinstance(entry, dict) else None

    def complete_domain(self, name: str, *, rows: int, replayed_ops: int,
                        legacy_digest: str) -> None:
        self._state.setdefault("domains", {})[name] = {
            "status": "complete",
            "rows": rows,
            "replayed_ops": replayed_ops,
            "legacy_digest": legacy_digest,
        }
        self._publish()

    def _publish(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)


# ── Migration driver ─────────────────────────────────────────────────────
def _legacy_domain_digest(
    spec: DomainSpec, documents: list[dict[str, Any]],
) -> tuple[int, str]:
    return len(documents), digest_documents(spec.legacy_projection(documents))


def _target_domain_digest(
    spec: DomainSpec, ctx: ReplayContext, documents: list[dict[str, Any]],
) -> tuple[int, str]:
    return len(documents), digest_documents(
        spec.target_projection(ctx, documents))


def run_migration(
    *,
    legacy_path: Path,
    session: StorageV2Session,
    checkpoint_path: Path,
    domain_names: Sequence[str] | None = None,
    fault_after_ops: int | None = None,
) -> dict[str, Any]:
    """Replay + verify; returns the machine-readable migration report."""
    registry = domain_registry()
    selected = ordered_domains(registry)
    if domain_names is not None:
        unknown = set(domain_names) - set(registry)
        if unknown:
            raise MigrationError(f"unknown domains: {sorted(unknown)}")
        wanted = set(domain_names)
        selected = [spec for spec in selected if spec.name in wanted]

    connection = open_readonly_sqlite_authority(legacy_path)
    checkpoint = Checkpoint(checkpoint_path)
    ctx = ReplayContext(session, fault_after_ops)
    report: dict[str, Any] = {
        "format": "tofudb-migration-report.v1",
        "source": str(legacy_path),
        "checkpoint": str(checkpoint_path),
        "domains": {},
        "ok": True,
    }
    try:
        for spec in selected:
            documents = list(spec.scan(connection))
            rows, legacy_digest = _legacy_domain_digest(spec, documents)
            prior = checkpoint.domain(spec.name)
            resumed = (
                prior is not None
                and prior.get("status") == "complete"
                and prior.get("rows") == rows
                and prior.get("legacy_digest") == legacy_digest
            )
            ops_before = ctx.replayed_ops
            if not resumed:
                spec.replay(ctx, documents)
                checkpoint.complete_domain(
                    spec.name, rows=rows,
                    replayed_ops=ctx.replayed_ops - ops_before,
                    legacy_digest=legacy_digest,
                )
            target_rows, target_digest = _target_domain_digest(
                spec, ctx, documents)
            match = rows == target_rows and legacy_digest == target_digest
            report["domains"][spec.name] = {
                "legacy_count": rows,
                "target_count": target_rows,
                "legacy_sha256": legacy_digest,
                "target_sha256": target_digest,
                "resumed_from_checkpoint": resumed,
                "replayed_ops": (
                    0 if resumed
                    else checkpoint.domain(spec.name)["replayed_ops"]
                ),
                "match": match,
            }
            if not match:
                report["ok"] = False
    finally:
        connection.close()
    return report


# ── CLI ──────────────────────────────────────────────────────────────────
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resumable legacy-SQLite → Tofu-DB migration gate")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", help="explicit legacy SQLite authority path")
    source.add_argument(
        "--data-dir",
        help="deployment data dir for fail-closed authority auto-resolution")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True,
                        help="tofu-db serve storage.v2 port")
    parser.add_argument(
        "--secret",
        help="storage.v2 shared secret (or TOFUDB_V2_SECRET env, 32..256 ASCII)")
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--tenant-id", type=int, default=None)
    parser.add_argument("--checkpoint", required=True,
                        help="durable resume cursor file")
    parser.add_argument("--report", help="write the JSON report here")
    parser.add_argument("--domains",
                        help="comma-separated subset of registered domains")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    secret = args.secret or os.environ.get("TOFUDB_V2_SECRET", "")
    if not 32 <= len(secret.encode("utf-8")) <= 256:
        print("tofudb-migrate: secret must be 32..256 bytes", file=sys.stderr)
        return 2
    try:
        if args.db:
            legacy_path = Path(args.db).expanduser().resolve()
            if not legacy_path.is_file():
                raise FileNotFoundError(f"legacy authority: {legacy_path}")
        else:
            location = resolve_readonly_sqlite_authority(args.data_dir)
            legacy_path = location.path
        fault_raw = os.environ.get(FAULT_ENV, "").strip()
        fault_after_ops = int(fault_raw) if fault_raw else None
        if fault_after_ops is not None and fault_after_ops <= 0:
            raise MigrationError(f"{FAULT_ENV} must be a positive integer")
        domains = (
            [name.strip() for name in args.domains.split(",") if name.strip()]
            if args.domains else None
        )
        with StorageV2Session(
            args.host, args.port, secret.encode("utf-8"),
            owner_id=args.owner_id, tenant_id=args.tenant_id, timeout=30.0,
        ) as session:
            report = run_migration(
                legacy_path=legacy_path,
                session=session,
                checkpoint_path=Path(args.checkpoint),
                domain_names=domains,
                fault_after_ops=fault_after_ops,
            )
    except (MigrationError, StorageV2Error, FileNotFoundError, OSError) as error:
        print(f"tofudb-migrate: {error}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        Path(args.report).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
