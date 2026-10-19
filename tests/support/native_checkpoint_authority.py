"""Selected native checkpoint transports over one already initialized authority.

Readiness and health report the requested WAL policy. The daemon stays in its
caller's process group so owned SIGKILL probes kill and join the actual writer. Ordinary exit closes stdin and joins before returning.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
import uuid
import time


@contextmanager
def checkpoint_authority(directory, *, transport='embedded', library, binary=None,
                         source_sha256, owner_id=2, tenant_id=1, durability='strict'):
    assert durability in {'strict', 'normal'}
    secret = b'x' * 32
    if transport == 'embedded':
        assert durability == 'strict'
        from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
        from tofu_db.embedded import EmbeddedRuntime
        with EmbeddedRuntime(Path(library), directory, owner_id=owner_id,
                             tenant_id=tenant_id, secret=secret) as runtime:
            assert runtime.build_identity()['source_sha256'] == source_sha256
            client = EmbeddedStorageV2Client(runtime, secret, owner_id=owner_id, tenant_id=tenant_id)
            try: yield client
            finally: client.close()
        return
    assert transport == 'daemon' and binary
    from lib.storage.v2_client import StorageV2Client
    build = json.loads(subprocess.check_output([str(binary), '--build-info'], text=True, timeout=10))
    assert build['source_sha256'] == source_sha256
    environment = dict(os.environ, TOFU_STORAGE_TOKEN=secret.decode())
    with (Path(directory).parent / 'checkpoint-daemon.log').open('ab') as log:
        process = subprocess.Popen([str(binary), 'serve', '--data-dir', str(directory),
            '--owner-id', str(owner_id), '--tenant-id', str(tenant_id), '--durability', durability],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=environment)
        exit_receipt = Path(directory).parent / ('checkpoint-daemon-exit-' + uuid.uuid4().hex + '.json')
        def join_writer():
            code = process.wait()
            staging = exit_receipt.with_suffix('.staging')
            staging.write_text(json.dumps(dict(pid=process.pid, returncode=code)) + '\n')
            staging.replace(exit_receipt)
        waiter = threading.Thread(target=join_writer, name='test-checkpoint-writer-join', daemon=True)
        waiter.start()
        client = None
        try:
            ready = bytearray()
            deadline = time.monotonic() + 30
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while b'\n' not in ready:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and selector.select(remaining), 'daemon readiness timed out'
                    data = os.read(process.stdout.fileno(), 4096)
                    assert data and len(ready) + len(data) <= 4096, 'invalid daemon readiness'
                    ready.extend(data)
            readiness = json.loads(ready.split(b'\n', 1)[0])
            assert readiness['walDurability'] == durability
            port = readiness['port']
            client = StorageV2Client('127.0.0.1', port, secret, owner_id=owner_id,
                                     tenant_id=tenant_id, timeout=30)
            assert client.health()['walDurability'] == durability
            assert client.native_checkpoint_available(user_id=owner_id)
            client._test_checkpoint_writer = dict(pid=process.pid, exit_receipt=str(exit_receipt), durability=durability)
            yield client
        finally:
            if client is not None: client.close()
            process.stdin.close()
            try: process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise AssertionError('daemon shutdown required a forced kill')
            finally:
                waiter.join(timeout=10)
                process.stdout.close()
                assert not waiter.is_alive(), 'checkpoint writer join did not finish'
            assert process.returncode == 0, f'checkpoint daemon exited {process.returncode}'
