"""tests/test_adapter_agent.py — CLIProxyAPI sidecar 看护器守卫（E4 agent 侧）。

Covers lib/desktop_agent/_adapter.py + the loopback target class in
lib/desktop_agent/_egress.py:

  * policy round-trip + adapter_loopback_port (drives the whitelist);
  * release asset selection per platform + no-plugin exclusion;
  * checksums.txt parsing + SHA-256 verify (mismatch NEVER swaps a binary);
  * config.yaml shape (loopback bind / keyed / management locked / panel off);
  * cmd_adapter_ensure happy path + download-failure path (mocked);
  * _pick_port collision bump;
  * loopback whitelist: right port ok, wrong port / no policy / public host
    with target=loopback / loopback host with target=subscription all refused.

Failure-first: the module did not exist before E4 (ImportError red).
"""

from __future__ import annotations

import io
import os
import tarfile
import unittest
import zipfile
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

from lib.desktop_agent import _adapter, _egress


def _tmp_config(tmp_path):
    """Point the agent config (and thus _adapter_root) at a temp dir."""
    cfg = str(tmp_path / 'desktop_agent.json')
    return mock.patch.dict(os.environ, {'TOFU_DESKTOP_CONFIG': cfg})


def _reset_module():
    _adapter._proc = None
    _adapter._proc_started_at = 0.0
    _adapter._update_available = ''


class TestPolicyAndWhitelist(unittest.TestCase):

    def setUp(self):
        _reset_module()

    def test_policy_roundtrip_and_loopback_port(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ,
                             {'TOFU_DESKTOP_CONFIG': os.path.join(td, 'c.json')}):
            self.assertEqual(_adapter.adapter_loopback_port(), 0)
            _adapter._write_policy({'port': 8317, 'api_key': 'k',
                                    'mgmt_secret': 'm', 'active': True})
            self.assertEqual(_adapter.adapter_loopback_port(), 8317)

    def test_loopback_whitelist_matrix(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ,
                             {'TOFU_DESKTOP_CONFIG': os.path.join(td, 'c.json')}):
            _adapter._write_policy({'port': 8317})
            ok = 'http://127.0.0.1:8317/v1/chat/completions'
            self.assertTrue(_egress._host_allowed(ok, 'loopback'))
            # Wrong port / no port / other loopback service → refused.
            self.assertFalse(_egress._host_allowed(
                'http://127.0.0.1:8318/v1/x', 'loopback'))
            self.assertFalse(_egress._host_allowed(
                'http://127.0.0.1/v1/x', 'loopback'))
            # Public host is NOT a loopback target.
            self.assertFalse(_egress._host_allowed(
                'https://api.anthropic.com/v1/messages', 'loopback'))
            # Loopback is NOT a subscription target.
            self.assertFalse(_egress._host_allowed(ok, 'subscription'))
            # No policy at all → refused.
            _adapter._write_policy({})
            self.assertFalse(_egress._host_allowed(ok, 'loopback'))

    def test_egress_http_command_passes_target_to_whitelist(self):
        # A loopback command without a policy must be refused by the agent
        # executor itself (defense in depth — the server check is not enough).
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ,
                             {'TOFU_DESKTOP_CONFIG': os.path.join(td, 'c.json')}):
            out = _egress.cmd_egress_http({
                'url': 'http://127.0.0.1:8317/v1/models',
                'method': 'GET', 'target': 'loopback'})
            self.assertIn('error', out)


class TestDownloadAndVerify(unittest.TestCase):

    def test_parse_checksums(self):
        # NOTE: adjacent-literal concatenation binds tighter than * — build
        # the two lines with explicit joins, not 'x'*64 + '...' 'y'*64.
        text = '\n'.join(['a' * 64 + '  CLIProxyAPI_7.2.116_linux_amd64.tar.gz',
                          'b' * 64 + '  CLIProxyAPI_7.2.116_windows_amd64.zip'])
        sums = _adapter._parse_checksums(text)
        self.assertEqual(sums['CLIProxyAPI_7.2.116_windows_amd64.zip'],
                         'b' * 64)

    def test_asset_selection_linux_and_no_plugin_excluded(self):
        assets = [
            {'name': 'CLIProxyAPI_7.2.116_linux_amd64_no-plugin.tar.gz',
             'browser_download_url': 'u1'},
            {'name': 'CLIProxyAPI_7.2.116_linux_amd64.tar.gz',
             'browser_download_url': 'u2'},
            {'name': 'checksums.txt', 'browser_download_url': 'u3'},
        ]
        name, url = _adapter._asset_for_platform(assets)
        self.assertEqual(name, 'CLIProxyAPI_7.2.116_linux_amd64.tar.gz')
        self.assertEqual(url, 'u2')

    def test_asset_selection_windows(self):
        assets = [{'name': 'CLIProxyAPI_7.2.116_windows_amd64.zip',
                   'browser_download_url': 'u9'}]
        with mock.patch.object(_adapter.sys, 'platform', 'win32'):
            name, _ = _adapter._asset_for_platform(assets)
        self.assertEqual(name, 'CLIProxyAPI_7.2.116_windows_amd64.zip')

    def test_asset_missing_raises(self):
        with self.assertRaises(ValueError):
            _adapter._asset_for_platform(
                [{'name': 'CLIProxyAPI_7.2.116_freebsd_amd64.tar.gz'}])

    def _fake_release_io(self, tmp_path, binary_body=b'#!/bin/sh\necho hi\n'):
        """Build a real tar.gz carrying a fake CLIProxyAPI binary."""
        import hashlib
        archive = tmp_path / 'CLIProxyAPI_7.2.116_linux_amd64.tar.gz'
        with tarfile.open(archive, 'w:gz') as t:
            # Real v7.2.116 tar member name (dashes!) — pinned after the
            # first live smoke caught a startswith('cliproxyapi') miss.
            info = tarfile.TarInfo('cli-proxy-api')
            info.size = len(binary_body)
            t.addfile(info, io.BytesIO(binary_body))
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        return archive, sha

    def test_install_binary_verified_and_swapped(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ,
                             {'TOFU_DESKTOP_CONFIG': os.path.join(td, 'c.json')}):
            archive, sha = self._fake_release_io(Path(td))
            assets = [
                {'name': 'checksums.txt', 'browser_download_url': 'sums'},
                {'name': archive.name, 'browser_download_url': 'asset'},
            ]
            sums_text = f'{sha}  {archive.name}\n'

            class _Resp:
                def __init__(self, text='', raw=b''):
                    self.text = text
                    self._raw = raw

                def raise_for_status(self):
                    return None

                def iter_content(self, n):
                    yield self._raw

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            def fake_gh(url, timeout=20):
                if url == 'sums':
                    return _Resp(text=sums_text)
                if url == 'asset':
                    return _Resp(raw=archive.read_bytes())
                raise AssertionError(url)

            with mock.patch.object(_adapter, '_resolve_release',
                                   return_value=('v7.2.116', assets)), \
                 mock.patch.object(_adapter, '_gh_get', side_effect=fake_gh):
                tag = _adapter.install_binary('latest')
            self.assertEqual(tag, 'v7.2.116')
            self.assertTrue(os.path.isfile(_adapter._binary_path()))
            self.assertEqual(_adapter._installed_version(), 'v7.2.116')

    def test_install_binary_hash_mismatch_never_swaps(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ,
                             {'TOFU_DESKTOP_CONFIG': os.path.join(td, 'c.json')}):
            archive, _sha = self._fake_release_io(Path(td))
            assets = [
                {'name': 'checksums.txt', 'browser_download_url': 'sums'},
                {'name': archive.name, 'browser_download_url': 'asset'},
            ]

            class _Resp:
                def __init__(self, text='', raw=b''):
                    self.text = text
                    self._raw = raw

                def raise_for_status(self):
                    return None

                def iter_content(self, n):
                    yield self._raw

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            def fake_gh(url, timeout=20):
                if url == 'sums':
                    return _Resp(text='0' * 64 + f'  {archive.name}\n')
                return _Resp(raw=archive.read_bytes())

            with mock.patch.object(_adapter, '_resolve_release',
                                   return_value=('v7.2.116', assets)), \
                 mock.patch.object(_adapter, '_gh_get', side_effect=fake_gh):
                with self.assertRaises(ValueError) as ctx:
                    _adapter.install_binary('latest')
            self.assertIn('SHA-256 mismatch', str(ctx.exception))
            self.assertFalse(os.path.isfile(_adapter._binary_path()))

    def test_zip_traversal_is_rejected_without_touching_destination(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / 'CLIProxyAPI_windows_amd64.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('../../CLIProxyAPI.exe', b'evil')
            dest = root / 'adapter'
            dest.mkdir()
            with self.assertRaisesRegex(ValueError, 'unsafe adapter archive path'):
                _adapter._extract_binary(str(archive), str(dest))
            self.assertFalse((root / 'CLIProxyAPI.exe').exists())
            self.assertEqual(list(dest.iterdir()), [])

    def test_tar_binary_install_is_atomic_and_leaves_no_staging_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive, _sha = self._fake_release_io(root, b'new binary')
            dest = root / 'adapter'
            dest.mkdir()
            final = dest / 'cliproxyapi'
            final.write_bytes(b'working old binary')
            installed = _adapter._extract_binary(str(archive), str(dest))
            self.assertEqual(Path(installed).read_bytes(), b'new binary')
            self.assertFalse(any(
                p.name.startswith('.cliproxyapi-') for p in dest.iterdir()))


class TestConfigAndLifecycle(unittest.TestCase):

    def setUp(self):
        _reset_module()

    def test_config_yaml_loopback_keyed_and_locked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ,
                             {'TOFU_DESKTOP_CONFIG': os.path.join(td, 'c.json')}):
            cfg = _adapter._write_config({
                'port': 8317, 'api_key': 'ta_abc', 'mgmt_secret': 'ms3'})
            with open(cfg) as f:
                text = f.read()
            self.assertIn('host: "127.0.0.1"', text)
            self.assertIn('port: 8317', text)
            self.assertIn('  - "ta_abc"', text)
            self.assertIn('secret-key: "ms3"', text)
            self.assertIn('disable-control-panel: true', text)
            self.assertIn('allow-remote: false', text)

    def test_pick_port_bumps_on_collision(self):
        import socket
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        s.listen(1)  # bound-but-not-listening still answers connect_ex refused
        taken = s.getsockname()[1]
        try:
            self.assertEqual(_adapter._pick_port(taken), taken + 1)
        finally:
            s.close()

    def test_ensure_happy_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ,
                             {'TOFU_DESKTOP_CONFIG': os.path.join(td, 'c.json')}):
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None
            fake_proc.pid = 4242

            def _fake_install(_v):
                with open(_adapter._version_file(), 'w') as f:
                    f.write('v7.2.116')
                return 'v7.2.116'
            with mock.patch.object(_adapter, 'install_binary',
                                   side_effect=_fake_install) as inst, \
                 mock.patch.object(_adapter, '_spawn',
                                   side_effect=lambda p: setattr(
                                       _adapter, '_proc', fake_proc)), \
                 mock.patch.object(_adapter, '_healthy', return_value=True), \
                 mock.patch.object(_adapter, '_start_supervisor'), \
                 mock.patch.object(_adapter, '_start_update_thread'):
                out = _adapter.cmd_adapter_ensure({
                    'port': 8317, 'api_key': 'ta_x', 'mgmt_secret': 'ms',
                    'version': 'latest', 'auto_update': True})
            self.assertTrue(out.get('running'))
            self.assertEqual(out.get('version'), 'v7.2.116')
            self.assertEqual(out.get('pid'), 4242)
            self.assertTrue(inst.called)
            # Policy persisted → resume path and loopback whitelist work.
            self.assertEqual(_adapter.adapter_loopback_port(), 8317)

    def test_ensure_download_failure_reports(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ,
                             {'TOFU_DESKTOP_CONFIG': os.path.join(td, 'c.json')}):
            with mock.patch.object(_adapter, 'install_binary',
                                   side_effect=ValueError('SHA-256 mismatch')):
                out = _adapter.cmd_adapter_ensure({
                    'port': 8317, 'api_key': 'ta_x', 'mgmt_secret': 'ms'})
            self.assertIn('SHA-256 mismatch', out.get('error', ''))

    def test_ensure_requires_credentials(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ,
                             {'TOFU_DESKTOP_CONFIG': os.path.join(td, 'c.json')}):
            out = _adapter.cmd_adapter_ensure({'port': 8317})
            self.assertIn('incomplete', out.get('error', ''))

    def test_stop_marks_inactive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ,
                             {'TOFU_DESKTOP_CONFIG': os.path.join(td, 'c.json')}):
            _adapter._write_policy({'port': 8317, 'active': True})
            out = _adapter.cmd_adapter_stop({})
            self.assertFalse(out.get('active'))
            self.assertFalse(_adapter._read_policy().get('active'))


class TestDispatchGating(unittest.TestCase):

    def test_adapter_commands_require_allow_egress(self):
        from lib.desktop_agent._dispatch import dispatch_command
        for cmd in ('adapter_ensure', 'adapter_status', 'adapter_stop'):
            out = dispatch_command(cmd, {}, {'allow_egress': False})
            self.assertIn('--allow-egress', out.get('error', ''))

    def test_adapter_commands_registered(self):
        from lib.desktop_agent._dispatch import COMMANDS
        for cmd in ('adapter_ensure', 'adapter_status', 'adapter_stop'):
            self.assertIn(cmd, COMMANDS)


if __name__ == '__main__':
    unittest.main()
