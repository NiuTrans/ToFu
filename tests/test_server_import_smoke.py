"""Server import-graph smoke (owner directive 2026-07-31).

The orchestrator decomposition (pt_03f4cdf1, 29+ leaf modules) means
``import server`` now traverses a long delegation chain. A single
broken link — a renamed symbol, a circular import, a missing module —
kills the server AT RESTART, which is precisely when nobody is
watching the test suite. This smoke runs ``import server`` in a
SUBPROCESS (never in-process: server.py bootstraps logging / TLS /
proxy detection at import time and must not pollute the pytest
process) and asserts a clean exit.

Cost: ~1s. Pinned into the standing orchestrator sweep.
"""

from __future__ import annotations

import subprocess
import sys


def test_server_import_graph_loads_cleanly():
    """`python -c "import server"` must exit 0 on the current tree.

    A non-zero exit means the import chain (server → routes → lib →
    orchestrator facade → 29 leaves) is broken — restart would be
    fatal. stderr is surfaced on failure for diagnosis.
    """
    proc = subprocess.run(
        [sys.executable, '-c', 'import server'],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, (
        f'`import server` failed (rc={proc.returncode}) — the import '
        f'chain is broken and a restart would be fatal.\n'
        f'stderr tail:\n{proc.stderr[-2000:]}')
