"""Private socket endpoints remain usable beyond Linux's raw sun_path limit."""
import os
from pathlib import Path
import socket

import pytest
from rootless_vm.unix_socket import connect_unix_socket

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('long_path', [False, True])
def test_private_socket_connects_without_shortening_its_owner_path(tmp_path, long_path):
    parent = tmp_path / ('private-' + 'x'*150 if long_path else 'private')
    parent.mkdir(mode=0o700)
    path = parent/'guest-agent.sock'
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.settimeout(1)
            listener.bind(f'/proc/self/fd/{descriptor}/{path.name}')
            listener.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                connect_unix_socket(client, path)
                peer, _ = listener.accept()
                with peer:
                    client.sendall(b'private')
                    assert peer.recv(7) == b'private'
        assert path.is_socket()
    finally:
        os.close(descriptor)


def test_directory_descriptor_closes_when_connection_fails(tmp_path, monkeypatch):
    opened = []
    original = os.open

    def track(path, flags, *args, **kwargs):
        descriptor = original(path, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, 'open', track)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        with pytest.raises(OSError):
            connect_unix_socket(client, tmp_path/'missing.sock')
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize('protocol', ['qmp', 'qga'])
def test_control_clients_use_long_private_endpoints(tmp_path, protocol):
    import json
    import threading
    from rootless_vm.guest_agent import GuestAgent
    from rootless_vm.session import SandboxSession

    parent = tmp_path / ('private-' + 'x'*150)
    parent.mkdir(mode=0o700)
    path = parent/(protocol+'.sock')
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    errors = []
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.settimeout(2)
            listener.bind(f'/proc/self/fd/{descriptor}/{path.name}')
            listener.listen(1)

            def serve():
                try:
                    peer, _ = listener.accept()
                    with peer, peer.makefile('rwb', buffering=0) as stream:
                        peer.settimeout(2)
                        if protocol == 'qmp':
                            stream.write(b'{"QMP":{}}\n')
                        for index in range(2):
                            request = json.loads(stream.readline().lstrip(b'\xff'))
                            value = (request['arguments']['id'] if protocol == 'qga' and index == 0
                                     else {} if index == 0 else {'ready': True})
                            stream.write((json.dumps({'return':value,'id':request['id']})+'\n').encode())
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                if protocol == 'qga':
                    result = GuestAgent(path).request('guest-ping', timeout=2)
                else:
                    session = object.__new__(SandboxSession)
                    session.qmp_path = path
                    result = session._qmp('query-chardev', timeout=2)
                assert result == {'ready': True}
            finally:
                worker.join(timeout=3)
            assert not worker.is_alive()
            assert not errors
    finally:
        os.close(descriptor)
