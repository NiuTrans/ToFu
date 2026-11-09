"""Local Gradle release signing validates the certificate, not a keystore filename.

Requires a JDK whose launcher runs source files (Java 11 or later); uses only
temporary synthetic keys and the public shipped debug certificate. It never
generates or configures a production signing identity.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def jdk():
    home = os.environ.get('JAVA_HOME', '')
    java = str(Path(home) / 'bin/java') if home else shutil.which('java')
    keytool = str(Path(home) / 'bin/keytool') if home else shutil.which('keytool')
    if not java or not keytool or not Path(java).is_file() or not Path(keytool).is_file():
        pytest.skip('JDK not available for local keystore verification')
    # The guard runs through the single-file source launcher (Java 11+).
    reported = subprocess.run([java, '-version'], capture_output=True, text=True, timeout=30).stderr
    found = re.search(r'version "(?:1\.)?(\d+)', reported)
    if not found or int(found.group(1)) < 11:
        pytest.skip(f'{java} cannot launch Java source files (Java 11 or later required)')
    return java, keytool


def _run_guard(jdk, key, alias, pin, *, password='fixture-password', environment_key=None, cwd=None):
    environment = dict(os.environ,
        TOFU_ANDROID_KEYSTORE_PATH=str(key if environment_key is None else environment_key),
        TOFU_ANDROID_KEYSTORE_PASSWORD=password,
        TOFU_ANDROID_KEY_ALIAS=alias,
        TOFU_ANDROID_KEY_PASSWORD=password,
        TOFU_ANDROID_SIGNER_SHA256=pin,
    )
    return subprocess.run([jdk[0], str(ROOT / 'android/ValidateKeystore.java'), str(key.resolve())],
                          env=environment, cwd=cwd, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('alias', ['androiddebugkey', 'AndroidDebugKey', 'ANDROIDDEBUGKEY'])
def test_a_copied_debug_keystore_cannot_be_a_release_identity(jdk, tmp_path, alias):
    copied = tmp_path / 'innocent-private-name.jks'
    shutil.copyfile(ROOT / 'android/app/debug.keystore', copied)
    certificate = subprocess.run([jdk[1], '-exportcert', '-keystore', str(copied),
                                  '-alias', alias, '-storepass', 'android'],
                                 check=True, capture_output=True, timeout=30).stdout
    result = _run_guard(jdk, copied, alias, hashlib.sha256(certificate).hexdigest(), password='android')
    assert result.returncode != 0
    assert 'debug' in result.stderr.lower()


def test_private_certificate_requires_its_exact_pin(jdk, tmp_path):
    key = tmp_path / 'fixture-private.jks'
    subprocess.run([jdk[1], '-genkeypair', '-keystore', str(key), '-storetype', 'PKCS12',
                    '-alias', 'fixture-release', '-storepass', 'fixture-password',
                    '-keypass', 'fixture-password', '-dname', 'CN=Fixture Release',
                    '-keyalg', 'RSA', '-keysize', '2048', '-validity', '1', '-noprompt'],
                   check=True, capture_output=True, timeout=30)
    certificate = subprocess.run([jdk[1], '-exportcert', '-keystore', str(key),
                                  '-alias', 'fixture-release', '-storepass', 'fixture-password'],
                                 check=True, capture_output=True, timeout=30).stdout
    result = _run_guard(jdk, key, 'fixture-release', hashlib.sha256(certificate).hexdigest())
    assert result.returncode == 0, result.stderr
    refused = _run_guard(jdk, key, 'fixture-release', 'a' * 64)
    assert refused.returncode != 0
    assert 'fingerprint' in refused.stderr.lower()
    wrong_password = _run_guard(jdk, key, 'fixture-release', 'a' * 64, password='fixture-never-echo-this')
    assert wrong_password.returncode != 0
    assert 'fixture-never-echo-this' not in wrong_password.stderr


def test_gradle_resolved_keystore_wins_over_cwd_and_same_named_file(jdk, tmp_path):
    """The guard checks the file AGP resolved, never an inherited relative path."""
    app = tmp_path / 'app'
    app.mkdir()
    for directory in (tmp_path, app):
        subprocess.run([jdk[1], '-genkeypair', '-keystore', str(directory / 'signer.jks'),
                        '-storetype', 'PKCS12', '-alias', 'fixture-release',
                        '-storepass', 'fixture-password', '-keypass', 'fixture-password',
                        '-dname', 'CN=Fixture Release', '-keyalg', 'RSA', '-keysize', '2048',
                        '-validity', '1', '-noprompt'],
                       check=True, capture_output=True, timeout=30)
    signing_file = app / 'signer.jks'
    certificate = subprocess.run([jdk[1], '-exportcert', '-keystore', str(signing_file),
                                  '-alias', 'fixture-release', '-storepass', 'fixture-password'],
                                 check=True, capture_output=True, timeout=30).stdout
    pin = hashlib.sha256(certificate).hexdigest()
    for cwd in (tmp_path, app):
        for environment_key in (Path('signer.jks'), signing_file):
            result = _run_guard(jdk, signing_file, 'fixture-release', pin,
                                environment_key=environment_key, cwd=cwd)
            assert result.returncode == 0, result.stderr
