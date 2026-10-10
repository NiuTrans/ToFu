"""Guest evidence publication and declared control-plane transport boundaries."""
from __future__ import annotations

import asyncio
import errno
import ipaddress
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_global_egress_gate_retains_constant_descriptor_budget(tmp_path):
    from rootless_vm.egress_proxy import _global_connection_slot

    descriptors = Path('/proc/self/fd')
    if not descriptors.exists():
        pytest.skip('Linux descriptor accounting')
    before = len(list(descriptors.iterdir()))
    with _global_connection_slot(tmp_path / 'gate', 64):
        with _global_connection_slot(tmp_path / 'gate', 64):
            assert len(list(descriptors.iterdir())) - before <= 6
    assert len(list(descriptors.iterdir())) == before


@pytest.mark.parametrize('failure_stage', ['acquire', 'release'])
def test_global_egress_gate_reclaims_descriptors_when_lock_fails(
        tmp_path, monkeypatch, failure_stage):
    import fcntl
    from rootless_vm.egress_proxy import _global_connection_slot

    descriptors = Path('/proc/self/fd')
    if not descriptors.exists():
        pytest.skip('Linux descriptor accounting')
    before = len(list(descriptors.iterdir()))

    flock = fcntl.flock

    def fail_lock(descriptor, operation):
        if (operation == fcntl.LOCK_UN) == (failure_stage == 'release'):
            raise OSError('injected lock failure')
        return flock(descriptor, operation)

    monkeypatch.setattr('rootless_vm.egress_proxy.fcntl.flock', fail_lock)
    with pytest.raises(OSError, match='injected lock failure'):
        with _global_connection_slot(tmp_path / 'gate', 64):
            assert failure_stage == 'release'
    assert len(list(descriptors.iterdir())) == before


class _DummyTunnel:
    def settimeout(self, _timeout):
        return None

    def close(self):
        return None


def test_https_upgrade_falls_back_to_hostname_connect_when_numeric_is_refused(
        monkeypatch):
    from rootless_vm import egress_proxy as ep

    seen = []
    tunnel = _DummyTunnel()

    monkeypatch.setattr(
        ep,
        '_safe_addresses_doh',
        lambda host, port, upstream: [(socket.AF_INET, ('1.2.3.4', port))],
    )

    def connect_through_upstream(_upstream, address, port):
        seen.append((address, port))
        try:
            ipaddress.ip_address(address)
        except ValueError:
            return tunnel
        raise OSError('numeric CONNECT refused')

    class _Tls:
        def wrap_socket(self, raw, server_hostname=None):
            assert raw is tunnel
            assert server_hostname == 'archive.ubuntu.com'
            return tunnel

    monkeypatch.setattr(ep, '_connect_through_upstream', connect_through_upstream)
    monkeypatch.setattr(ep.ssl, 'create_default_context', lambda: _Tls())

    result = ep._connect_plain_http(
        'archive.ubuntu.com', 80, 'http://proxy.example:3128',
    )
    assert result is tunnel
    assert seen == [('1.2.3.4', 443), ('archive.ubuntu.com', 443)]


def test_connect_public_falls_back_to_hostname_connect_when_numeric_is_refused(
        monkeypatch):
    from rootless_vm import egress_proxy as ep

    seen = []
    tunnel = _DummyTunnel()

    monkeypatch.setattr(
        ep,
        '_safe_addresses_doh',
        lambda host, port, upstream: [(socket.AF_INET, ('1.2.3.4', port))],
    )

    def connect_through_upstream(_upstream, address, port):
        seen.append((address, port))
        try:
            ipaddress.ip_address(address)
        except ValueError:
            return tunnel
        raise OSError('numeric CONNECT refused')

    monkeypatch.setattr(ep, '_connect_through_upstream', connect_through_upstream)

    result = ep._connect_public('pypi.org', 443, 'http://proxy.example:3128')
    assert result is tunnel
    assert seen == [('1.2.3.4', 443), ('pypi.org', 443)]


def test_ip_literal_https_upgrade_does_not_retry_the_address_as_a_hostname(
        monkeypatch):
    from rootless_vm import egress_proxy as ep

    seen = []

    monkeypatch.setattr(
        ep,
        '_safe_addresses_doh',
        lambda host, port, upstream: [(socket.AF_INET, ('8.8.8.8', port))],
    )

    def connect_through_upstream(_upstream, address, port):
        seen.append((address, port))
        raise OSError('numeric CONNECT refused')

    monkeypatch.setattr(ep, '_connect_through_upstream', connect_through_upstream)

    with pytest.raises(OSError, match='cannot securely upgrade'):
        ep._connect_plain_http('8.8.8.8', 80, 'http://proxy.example:3128')
    assert seen == [('8.8.8.8', 443)]


@pytest.mark.parametrize('declared', [False, True])
def test_only_declared_guest_service_bypasses_public_egress(declared):
    pytest.importorskip('harbor')
    from urllib.request import proxy_bypass_environment
    from rootless_vm.harbor_environment import RootlessQemuEnvironment
    from rootless_vm.session import LoopbackServiceForward

    service = LoopbackServiceForward(name='benchmark-proxy', guest_port=8765, host_port=12345)
    environment = RootlessQemuEnvironment.__new__(RootlessQemuEnvironment)
    environment._session = SimpleNamespace(
        egress_proxy=SimpleNamespace(proxy_url='http://rootless:placeholder@10.0.2.100:3128'))
    environment._loopback_services = (service,) if declared else ()
    variables = environment._network_environment()
    assert variables['no_proxy'] == variables['NO_PROXY']
    proxies = {'no': variables['no_proxy']}
    assert proxy_bypass_environment(f'{service.guest_host}:8765', proxies) is declared
    assert not proxy_bypass_environment('example.org:443', proxies)
    assert not proxy_bypass_environment('10.0.2.2:8080', proxies)
    assert not proxy_bypass_environment('10.1.1.1:8765', proxies)


@pytest.mark.parametrize('failure', [None, 'download', 'publish'])
def test_guest_file_download_across_mounts_preserves_existing_evidence(
        tmp_path, monkeypatch, failure):
    pytest.importorskip('harbor')
    from rootless_vm.harbor_environment import RootlessQemuEnvironment

    destination_volume = tmp_path / 'artifact-volume'
    destination_volume.mkdir()
    target = destination_volume / 'evidence.json'
    target.write_bytes(b'previous-complete-evidence')
    downloaded = []

    def download(source, staged, *, max_bytes):
        assert source == '/guest-rootfs/evidence.json'
        assert max_bytes == 1024
        downloaded.append(Path(staged))
        Path(staged).write_bytes(b'new-complete-evidence')
        if failure == 'download':
            raise OSError('injected interrupted download')

    replace = os.replace

    def publish(source, destination):
        # Model a distinct mounted artifact volume, as on the evaluation host.
        if not Path(source).is_relative_to(destination_volume):
            raise OSError(errno.EXDEV, 'Invalid cross-device link')
        if failure == 'publish':
            raise OSError('injected publication failure')
        replace(source, destination)

    monkeypatch.setattr('rootless_vm.harbor_environment.os.replace', publish)
    environment = RootlessQemuEnvironment.__new__(RootlessQemuEnvironment)
    environment._container_backend = 'runc'
    environment._guest_rootfs = '/guest-rootfs'
    environment._session = SimpleNamespace(guest_agent=SimpleNamespace(download=download))
    environment._transfer_limit_bytes = 1024
    if failure:
        with pytest.raises(OSError, match='injected'):
            asyncio.run(environment.download_file('/evidence.json', target))
        assert target.read_bytes() == b'previous-complete-evidence'
    else:
        asyncio.run(environment.download_file('/evidence.json', target))
        assert target.read_bytes() == b'new-complete-evidence'
    assert downloaded and all(not path.exists() for path in downloaded)
    assert list(destination_volume.iterdir()) == [target]
