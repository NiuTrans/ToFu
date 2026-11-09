"""Release validation is executable, not only a workflow text guard."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def validator():
    spec = importlib.util.spec_from_file_location('android_release_validator', ROOT / 'android/validate_release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('output', [
    '',
    'Signer #1 certificate SHA-256 digest: ' + 'b' * 64,
    'Signer #1 certificate SHA-256 digest: ' + 'a' * 64 + '\nSigner #2 certificate SHA-256 digest: ' + 'a' * 64,
    'Signer #1 certificate DN: CN=Android Debug\nSigner #1 certificate SHA-256 digest: ' + 'a' * 64,
])
def test_release_rejects_unknown_multiple_and_debug_signers(output):
    with pytest.raises(ValueError):
        validator().validate_certificate(output, 'a' * 64)


def test_release_accepts_exactly_one_pinned_certificate():
    # The one pinned non-debug signer passes validation; no exception is raised.
    assert validator().validate_certificate(
        'Signer #1 certificate SHA-256 digest: ' + 'a' * 64, ':'.join(['aa'] * 32)) is None


@pytest.mark.parametrize('failure', [None, 'signature', 'manifest', 'debuggable', 'tool'])
def test_validator_main_emits_receipt_only_after_all_native_checks(monkeypatch, tmp_path, failure):
    module = validator()
    source = (ROOT / 'android/app/build.gradle.kts').read_text()
    import re
    version = re.search(r'versionName\s*=\s*"([^"]+)"', source).group(1)
    code = re.search(r'versionCode\s*=\s*(\d+)', source).group(1)
    key = tmp_path / 'private-key.jks'
    key.write_bytes(b'synthetic-private-key')
    apk = tmp_path / 'tofu-android.apk'
    apk.write_bytes(b'synthetic-apk-for-control-flow-not-an-installable-build')
    receipt = tmp_path / 'published-release.json'
    for name, value in {
        'TOFU_ANDROID_KEYSTORE_PATH': str(key),
        'TOFU_ANDROID_KEYSTORE_PASSWORD': 'fixture-never-log-store-secret',
        'TOFU_ANDROID_KEY_ALIAS': 'private-release',
        'TOFU_ANDROID_KEY_PASSWORD': 'fixture-never-log-key-secret',
        'TOFU_ANDROID_SIGNER_SHA256': 'a' * 64,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, 'argv', ['validate_release.py', '--apk', str(apk),
                                    '--apksigner', 'fixture-signer', '--aapt', 'fixture-aapt',
                                    '--tag', 'android-v' + version, '--receipt', str(receipt)])
    calls = []
    def native_tool(command, **kwargs):
        calls.append(command)
        assert kwargs['check'] is True
        if failure == 'tool':
            raise subprocess.CalledProcessError(1, command, stderr='fixture-never-log-key-secret')
        if command[0] == 'fixture-signer':
            output = 'Signer #1 certificate SHA-256 digest: ' + ('b' if failure == 'signature' else 'a') * 64
        else:
            package = 'com.attacker.client' if failure == 'manifest' else 'com.tofu.client'
            output = f"package: name='{package}' versionCode='{code}' versionName='{version}'"
            if failure == 'debuggable':
                output += '\napplication-debuggable'
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')
    monkeypatch.setattr(module.subprocess, 'run', native_tool)
    if failure:
        with pytest.raises(SystemExit) as raised:
            module.main()
        assert raised.value.code == 1
        assert not receipt.exists()
    else:
        assert module.main() == 0
        value = json.loads(receipt.read_text())
        assert value['versionName'] == version
        assert value['versionCode'] == int(code)
        assert value['sha256'] == hashlib.sha256(apk.read_bytes()).hexdigest()
        assert value['sizeBytes'] == apk.stat().st_size
        assert value['url'].endswith('/android-v' + version + '/tofu-android.apk')
        assert [call[0] for call in calls] == ['fixture-signer', 'fixture-aapt']
