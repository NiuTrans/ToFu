"""PostgreSQL instance-ownership admin CLI.

Discoverable replacement for the old "remember to ``rm`` the ownership
markers" checklist that everyone forgot when copying the project to a new
path / machine / open-source clone.

Why this exists
---------------
Ownership markers (``.pg_owner_host``, ``.tofu_heartbeat``,
``postmaster.pid``) live INSIDE ``data/pgdata/`` — the same directory people
copy. A copied pgdata trusts those markers and silently routes every DB call
back to the ORIGINAL machine's PostgreSQL over FUSE (shared data + privacy
leak + no error shown).

Startup already self-heals this automatically via the ``.pg_instance_id``
path-stamp (see ``_bootstrap._heal_if_copied``). This CLI is the manual
escape hatch for the one case the stamp cannot disambiguate — a copy to the
EXACT same absolute path on a different machine — and a way to inspect state.

Usage
-----
    python -m lib.database.pg_admin status
    python -m lib.database.pg_admin reset-ownership [--yes]

``reset-ownership`` clears the markers but NEVER touches the data files, so
the next ``server.py`` boot starts/takes over PG locally on the fresh copy.
"""

import argparse
import os
import sys

from lib.log import get_logger
# Ownership functions relocated to _pg_ownership (Decoupling D, 2026-07-11).
# pg_admin uses ONLY ownership symbols, so bind directly to the canonical
# module — this keeps pg_admin and its tests patching the SAME namespace (a
# facade re-export in _bootstrap is a separate binding that monkeypatch on the
# canonical module would not intercept).
from lib.database import _pg_ownership as b

logger = get_logger(__name__)

# Mirror _core._PGDATA without importing _core (which has heavy import-time
# side effects — it bootstraps PG on import). Uses the writable data root so a
# frozen desktop build points at the same pgdata the server actually uses.
from lib.runtime_paths import data_root
_DEFAULT_PGDATA = os.path.join(data_root(), 'pgdata')


def _resolve_pgdata(arg):
    return arg or os.environ.get('TOFU_PGDATA') or _DEFAULT_PGDATA


def cmd_status(pgdata):
    """Print the ownership state of a pgdata directory."""
    canon = b._canonical_pgdata_path(pgdata)
    print(f'pgdata:           {pgdata}')
    print(f'canonical path:   {canon}')
    if not os.path.isdir(pgdata):
        print('state:            (directory does not exist — a fresh PG would be initdb\'d)')
        return 0

    stamp = b._read_instance_stamp(pgdata)
    if stamp:
        print(f'instance id:      {stamp.get("id")}')
        print(f'stamped path:     {b._canonical_pgdata_path(stamp.get("path", ""))}')
    else:
        print('instance id:      (none — legacy pgdata predating the stamp)')

    owner = b._read_pg_host_from_pidfile(pgdata)
    print(f'owner_host:       {owner or "(none)"}')
    fresh, hb = b._heartbeat_is_fresh(pgdata)
    if hb:
        print(f'heartbeat:        host={hb.get("host")} pid={hb.get("pid")} '
              f'age={hb.get("age_s", -1):.0f}s fresh={fresh}')
    else:
        print('heartbeat:        (none)')

    was_copied, stamped = b._pgdata_was_copied(pgdata)
    if was_copied:
        print()
        print(f'>>> COPIED: this pgdata was stamped at {stamped} but now lives '
              f'at {canon}.')
        print('>>> Startup will auto-heal (ignore inherited markers, take over '
              'locally). No action needed.')
    elif owner and not fresh:
        print()
        print('>>> Markers present but heartbeat is stale — startup will take '
              'over locally.')
    return 0


def cmd_reset_ownership(pgdata, assume_yes):
    """Clear ownership markers so the next boot owns PG locally."""
    if not os.path.isdir(pgdata):
        print(f'No pgdata directory at {pgdata} — nothing to reset.')
        return 0

    owner = b._read_pg_host_from_pidfile(pgdata)
    print(f'About to clear ownership markers in: {b._canonical_pgdata_path(pgdata)}')
    print(f'  current owner_host marker: {owner or "(none)"}')
    print('  will remove: .pg_owner_host, .tofu_heartbeat, postmaster.pid, '
          'postmaster.opts')
    print('  data files are NOT touched.')

    pidfile = os.path.join(pgdata, 'postmaster.pid')
    if os.path.exists(pidfile) and b._pidfile_pid_is_live_local_postgres(pgdata):
        print()
        print('REFUSING: postmaster.pid points at a LIVE local postgres on THIS '
              'machine. Stop the server (and its PG) first — removing the pidfile '
              'under a running postmaster risks corruption.')
        return 2

    if not assume_yes:
        try:
            resp = input('Proceed? [y/N] ').strip().lower()
        except EOFError as e:
            logger.debug('[pg_admin] no interactive stdin for confirm prompt: %s', e)
            resp = ''
        if resp not in ('y', 'yes'):
            print('Aborted.')
            return 1

    removed = b._clear_ownership_markers(pgdata, remove_pidfile=True,
                                         reason='reset-ownership CLI')
    print(f'Removed: {", ".join(removed) if removed else "(nothing — already clean)"}')
    print('Done. The next `python server.py` will start/own PG locally on this copy.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m lib.database.pg_admin',
        description='Inspect / reset PostgreSQL instance ownership markers.')
    parser.add_argument('--pgdata', default=None,
                        help='Path to the pgdata directory (default: data/pgdata '
                             'under the project root, or $TOFU_PGDATA).')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='Show ownership state of the pgdata directory.')
    p_reset = sub.add_parser(
        'reset-ownership',
        help='Clear ownership markers (keeps data) so the next boot owns PG locally.')
    p_reset.add_argument('--yes', '-y', action='store_true',
                         help='Skip the confirmation prompt.')

    args = parser.parse_args(argv)
    pgdata = _resolve_pgdata(args.pgdata)

    if args.command == 'status':
        return cmd_status(pgdata)
    if args.command == 'reset-ownership':
        return cmd_reset_ownership(pgdata, args.yes)
    parser.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
