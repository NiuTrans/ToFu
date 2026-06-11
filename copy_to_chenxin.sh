#!/usr/bin/env bash
#
# Copy the sanitized internal export (tofu-your-provider) to chenxin's directory.
#
# DEFAULT IS A DRY-RUN. Remove --dry-run (or run with APPLY=1) to actually copy.
#
# Why the excludes matter: data/pgdata/ carries this machine's PostgreSQL
# ownership markers (.pg_owner_host / postmaster.pid). If copied, chenxin's
# instance would silently connect back to THIS machine's PG over FUSE
# (data leak + postmaster.pid duel). The new copy must bootstrap its own PG,
# so pgdata is excluded and will be re-created by `python3 server.py`.
set -euo pipefail

SRC="/path/to/your/project"
DST="/mnt/your-fs"

# APPLY=1 ./copy_to_chenxin.sh  → real run.  Otherwise dry-run.
DRYRUN="--dry-run"
if [[ "${APPLY:-0}" == "1" ]]; then
  DRYRUN=""
fi

mkdir -p "$DST"

rsync -av --delete $DRYRUN \
  --exclude='data/pgdata/' \
  --exclude='data/*.db' \
  --exclude='logs/' \
  --exclude='__pycache__/' \
  --exclude='.pg_owner_host' \
  --exclude='postmaster.pid' \
  "$SRC" "$DST"

echo
if [[ -n "$DRYRUN" ]]; then
  echo "DRY-RUN only. Re-run with:  APPLY=1 $0"
else
  echo "Done. On the new copy, start fresh PG with:  cd $DST && python3 server.py"
fi
