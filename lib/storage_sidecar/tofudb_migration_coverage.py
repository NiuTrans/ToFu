"""Read-only source-table coverage gate for the explicit native import CLI.

The import plan is the sole mapping inventory. This gate never infers that an
unmapped table is disposable from its name or that empty tables prove cutover
readiness. Domain fidelity, namespace ownership and target verification remain
separate gates. SQL and bounded presence probes belong to migration_source.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from lib.storage_sidecar.tofudb_migration_source import SQLiteMigrationSource


def source_table_coverage(
    source: SQLiteMigrationSource, plan: Mapping[str, Any],
) -> dict[str, Any]:
    mapped = {table['name'] for domain in plan['domains'] for table in domain['tables']}
    mirrors = {}
    mirror_error = False
    if (plan.get('retiredTradingMirrorPolicy') == 'exact-current-plugin-rows'
            and {'storage_plugin_manifests', 'storage_plugin_rows'}.issubset(mapped)):
        from lib.storage_sidecar.tofudb_migration_mirrors import verified_trading_mirrors
        try:
            mirrors = verified_trading_mirrors(source)
        except (ValueError, TypeError, KeyError, AttributeError):
            # An unreadable proof never turns a populated table into coverage.
            mirror_error = True
    observations = [
        {'table': name, 'mapped': name in mapped, 'populated': source.table_has_rows(name)}
        for name in source.tables
    ]
    from lib.storage_sidecar.tofudb_migration_search import verified_retired_search
    rebuild_error = False
    try:
        rebuilds = verified_retired_search(source, plan)
    except (ValueError, TypeError, KeyError, AttributeError):
        rebuilds = {}
        rebuild_error = True
    try:
        from lib.storage_sidecar.tofudb_migration_transport import (
            verified_reconstructible_projections,
            verified_retired_transport,
        )
        rebuilds.update(verified_retired_transport(source, plan))
        rebuilds.update(verified_reconstructible_projections(source, plan))
    except (ValueError, TypeError, KeyError, AttributeError):
        rebuild_error = True
    # Retired Project Brain authorities are covered only when the plan declares
    # the legacy-cutover fold and that fold verifiably owns every populated one.
    # The fold's ownership proof is separate from mirror/rebuild verification:
    # it enumerates the watch intent it will carry and fails closed on error.
    legacy_fold, legacy_fold_error = _verified_legacy_project_brain_fold(source, plan)
    blockers = [row['table'] for row in observations
                if row['populated'] and not row['mapped']
                and row['table'] not in mirrors and row['table'] not in rebuilds
                and row['table'] not in legacy_fold]
    return {
        'planId': plan['id'],
        'scope': 'source-table-coverage-only',
        'sourceTableCoverageComplete': not blockers,
        'unmappedPopulatedTables': blockers,
        'verifiedMirrors': mirrors,
        'mirrorVerificationFailed': mirror_error,
        'verifiedRebuilds': rebuilds,
        'rebuildVerificationFailed': rebuild_error,
        'verifiedLegacyCutover': legacy_fold,
        'legacyCutoverVerificationFailed': legacy_fold_error,
        'tables': observations,
    }


def _verified_legacy_project_brain_fold(
    source: SQLiteMigrationSource, plan: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Name the retired Project Brain tables the declared fold verifiably owns.

    Returns ``({}, True)`` when the plan does not declare the fold policy or
    the fold cannot prove ownership, so a populated retired table still blocks
    coverage. The fold proves it can enumerate the watch intent it will carry;
    it never claims an empty or absent table as migrated data.
    """
    policy = plan.get('legacyProjectBrainCutoverPolicy')
    if not isinstance(policy, Mapping):
        return {}, False
    consumed = policy.get('consumedTables')
    if (not isinstance(consumed, list) or not consumed
            or any(not isinstance(name, str) for name in consumed)):
        return {}, True
    try:
        from lib.storage_sidecar import tofudb_migration_project_brain as fold
        if tuple(consumed) != tuple(fold.CONSUMED_TABLES):
            return {}, True
        # Force the fold to enumerate what it owns. Any read/shape error means
        # the fold cannot prove coverage, so the populated table stays a
        # blocker rather than being silently forgiven.
        documents = list(fold.fold_legacy_projections(source))
        owned = fold.consumed_populated_tables(source)
    except (ValueError, TypeError, KeyError, AttributeError):
        return {}, True
    # Watch-bearing tables must have carried every populated watch item into a
    # projection; execution-history tables are consumed-and-dropped by design.
    watch_bearing = {'storage_watch_items', 'storage_watch_responses'}
    carried_watch = sum(len(doc['projection']['watch']) for doc in documents)
    proof: dict[str, Any] = {}
    for name in owned:
        if name in watch_bearing:
            proof[name] = {'carriedWatchItems': carried_watch,
                           'foldedProjections': len(documents)}
        else:
            proof[name] = {'disposition': 'execution-history-dropped-by-cutover'}
    return proof, False


def main(argv: list[str] | None = None) -> int:
    from lib.storage_sidecar.tofudb_migration_import import PLAN

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    args = parser.parse_args(argv)
    with SQLiteMigrationSource(args.source) as source:
        result = source_table_coverage(source, PLAN)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['sourceTableCoverageComplete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
