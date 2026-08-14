#!/usr/bin/env python3
"""Thin command shim; driver/path authority remains in storage_sidecar."""

from pathlib import Path
import sys


# Direct script execution places ``scripts/`` rather than the repository root
# on sys.path.  Resolve the source package without depending on the caller's
# working directory; installed entry points do not need this compatibility
# shim.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.storage_sidecar.cli import main


if __name__ == '__main__':
    raise SystemExit(main())
