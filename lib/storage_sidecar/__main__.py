"""Sidecar process entry point.  The stdout stream is a parent control pipe."""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading

from lib.storage.errors import StorageError
from lib.log import get_logger
from lib.storage.protocol import PROTOCOL_VERSION
from lib.storage_sidecar.adapters import create_backend
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.preflight import ProjectLease
from lib.storage_sidecar.server import create_server


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    log = get_logger('tofu.storage.sidecar')
    backend = None
    lease = None
    server = None
    try:
        config = SidecarConfig.from_environment()
        lease = ProjectLease(config.data_dir)
        lease.acquire()
        backend = create_backend(config)
        backend.start()
        server = create_server(backend, config.token)

        def request_stop(_signum=None, _frame=None):
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        ready = {
            'type': 'storage.ready',
            'protocol': PROTOCOL_VERSION,
            'port': int(server.server_address[1]),
            'backend': config.backend,
        }
        # This is the only stdout message.  It intentionally contains neither
        # the token nor database paths/credentials.
        sys.stdout.write(json.dumps(ready, separators=(',', ':')) + '\n')
        sys.stdout.flush()
        server.serve_forever(poll_interval=0.2)
        return 0
    except StorageError as exc:
        log.critical('storage startup refused code=%s diagnostic=%s', exc.code, exc.message)
        return 2
    except BaseException as exc:
        log.critical('storage startup failed type=%s', type(exc).__name__, exc_info=True)
        return 2
    finally:
        if server is not None:
            server.server_close()
        if backend is not None:
            backend.close()
        if lease is not None:
            lease.release()


if __name__ == '__main__':
    raise SystemExit(main())
