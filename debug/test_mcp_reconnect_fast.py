#!/usr/bin/env python3
"""Regression test for the MCP reconnect-hang bug.

Before the owner-task refactor, calling ``connect_server`` with a name that
was already connected would block for ~130s because ``aclose()`` on the
old ``AsyncExitStack`` was awaited from a *different* task than the one
that opened it, causing anyio's cancel-scope/task mismatch to deadlock
until the outer ``MCP_CALL_TIMEOUT + 10`` budget fired.

With the fix, each server has a long-lived owner coroutine that opens
AND closes its own ``AsyncExitStack``. A reconnect should therefore
complete in well under a second plus the usual handshake cost.

Run with:
    python3 debug/test_mcp_reconnect_fast.py
"""

from __future__ import annotations

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger

logger = get_logger(__name__)


# Hard ceiling for a full reconnect cycle on a healthy machine. The old
# bug produced ~130s; a healthy reconnect should be well under 20s
# (connect handshake + list_tools, twice).
MAX_RECONNECT_BUDGET_SEC = 30.0


def main() -> int:
    if not shutil.which('npx'):
        print('⏭️  SKIP: npx not found — cannot run live reconnect test')
        return 0

    from lib.mcp.client import MCPBridge

    bridge = MCPBridge()
    srv_cfg = {
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
        'transport': 'stdio',
    }

    try:
        # First connect — establishes the baseline.
        t0 = time.time()
        tools1 = bridge.connect_server('fs_test', srv_cfg)
        t1 = time.time()
        assert tools1, 'Initial connect should return tools'
        print(f'  Initial connect: {t1 - t0:.2f}s ({len(tools1)} tools)')

        # Reconnect — this is the path that used to hang for 130s.
        t2 = time.time()
        tools2 = bridge.connect_server('fs_test', srv_cfg)
        t3 = time.time()
        elapsed = t3 - t2
        print(f'  Reconnect:       {elapsed:.2f}s ({len(tools2)} tools)')

        assert tools2, 'Reconnect should return tools'
        if elapsed > MAX_RECONNECT_BUDGET_SEC:
            print(f'❌ Reconnect took {elapsed:.2f}s — regression! '
                  f'(budget {MAX_RECONNECT_BUDGET_SEC}s)')
            return 1

        print(f'✅ Reconnect completed in {elapsed:.2f}s (well under '
              f'{MAX_RECONNECT_BUDGET_SEC}s budget)')
        return 0
    finally:
        t0 = time.time()
        bridge.disconnect_all()
        print(f'  disconnect_all:  {time.time() - t0:.2f}s')


if __name__ == '__main__':
    sys.exit(main())
