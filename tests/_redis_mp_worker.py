#!/usr/bin/env python3
"""Worker subprocess for the multi-pROCESS Redis integration test.

Launched as a real OS process by tests/test_redis_multiprocess.py. Connects to
the shared TcpFakeServer Redis over a SOCKET via TOFU_RUNTIME_STATE_BACKEND=
redis + TOFU_REDIS_URL. Usage:

  python _redis_mp_worker.py <mode> <principle> <limit> <ttl> <n>

Modes:
  acquire: try to acquire `n` distinct slots for `principle` at `limit`/`ttl`;
           print one line per acquire result (ADMIT/DENY <slot>), then a final
           "ADMITTED <k>" summary.
  hold:    acquire `n` slots then sleep forever (holding them) - the SIGKILL
           target. Prints "HELD <k>" once slots are held, then blocks.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    mode = sys.argv[1]
    principle = sys.argv[2]
    limit = int(sys.argv[3])
    ttl = float(sys.argv[4])
    n = int(sys.argv[5])
    tag = sys.argv[6] if len(sys.argv) > 6 else str(os.getpid())
    from lib.runtime_state_store import get_store, reset_for_test
    reset_for_test()
    store = get_store()
    prefix = principle + '::'
    kind = 'sse'
    if mode == 'acquire':
        admitted = 0
        for i in range(n):
            slot = '%s__%s__%d' % (prefix, tag, i)
            ok = store.acquire_slot(kind, slot, limit=limit, ttl=ttl, count_prefix=prefix)
            print('%s %s' % ('ADMIT' if ok else 'DENY', slot), flush=True)
            if ok:
                admitted += 1
        print('ADMITTED %d' % admitted, flush=True)
    elif mode == 'hold':
        held = 0
        for i in range(n):
            slot = '%s__%s__%d' % (prefix, tag, i)
            if store.acquire_slot(kind, slot, limit=limit, ttl=ttl, count_prefix=prefix):
                held += 1
        print('HELD %d' % held, flush=True)
        while True:
            time.sleep(3600)
    elif mode == 'count':
        print('COUNT %d' % store.count_slots(kind, prefix), flush=True)


if __name__ == '__main__':
    main()
