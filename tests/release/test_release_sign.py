"""Ed25519 release-manifest signing must round-trip and fail closed.

The packaging gate ships ``SHA256SUMS`` + ``SHA256SUMS.sig`` + a public key.
These tests pin: a real sign/verify round-trip, and that every tamper or
malformation is a NON-zero exit — never a silent "unsigned but okay".
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / 'scripts' / 'release_sign.py'

pytestmark = pytest.mark.unit


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True)


def _keypair(tmp_path):
    priv = tmp_path / 'release_private.pem'
    pub = tmp_path / 'release_signing_public_key.pem'
    result = _run('keygen', '--private', str(priv), '--public', str(pub))
    assert result.returncode == 0, result.stderr
    return priv, pub


def _manifest(tmp_path, body=b'abc123  Tofu-Setup-1.0.0-win64.exe\n'):
    manifest = tmp_path / 'SHA256SUMS'
    manifest.write_bytes(body)
    return manifest


def test_keygen_sign_verify_round_trip(tmp_path):
    priv, pub = _keypair(tmp_path)
    manifest = _manifest(tmp_path)
    sig = tmp_path / 'SHA256SUMS.sig'

    signed = _run('sign', '--manifest', str(manifest),
                  '--private', str(priv), '--out', str(sig))
    assert signed.returncode == 0, signed.stderr
    assert sig.read_bytes().__len__() == 64

    verified = _run('verify', '--manifest', str(manifest),
                    '--signature', str(sig), '--public', str(pub))
    assert verified.returncode == 0, verified.stderr
    assert 'authentic' in verified.stdout


def test_keygen_refuses_to_overwrite_without_force(tmp_path):
    priv, pub = _keypair(tmp_path)
    again = _run('keygen', '--private', str(priv), '--public', str(pub))
    assert again.returncode == 1
    assert 'REFUSED' in again.stderr
    # --force rotates (and warns that old signatures are invalidated).
    rotated = _run('keygen', '--private', str(priv), '--public', str(pub),
                   '--force')
    assert rotated.returncode == 0, rotated.stderr


def test_private_key_is_owner_readonly(tmp_path):
    priv, _ = _keypair(tmp_path)
    assert (priv.stat().st_mode & 0o777) == 0o600


def test_tampered_manifest_fails_closed(tmp_path):
    priv, pub = _keypair(tmp_path)
    manifest = _manifest(tmp_path)
    sig = tmp_path / 'SHA256SUMS.sig'
    assert _run('sign', '--manifest', str(manifest),
                '--private', str(priv), '--out', str(sig)).returncode == 0
    # Attacker rewrites the manifest after signing.
    manifest.write_bytes(b'deadbeef  Tofu-Setup-1.0.0-win64.exe\n')
    verified = _run('verify', '--manifest', str(manifest),
                    '--signature', str(sig), '--public', str(pub))
    assert verified.returncode == 1
    assert 'does NOT verify' in verified.stderr


def test_wrong_public_key_fails_closed(tmp_path):
    priv, _ = _keypair(tmp_path)
    # Second, unrelated keypair.
    other_dir = tmp_path / 'other'
    other_dir.mkdir()
    opriv = other_dir / 'p.pem'
    opub = other_dir / 'pub.pem'
    assert _run('keygen', '--private', str(opriv), '--public', str(opub)).returncode == 0

    manifest = _manifest(tmp_path)
    sig = tmp_path / 'SHA256SUMS.sig'
    assert _run('sign', '--manifest', str(manifest),
                '--private', str(priv), '--out', str(sig)).returncode == 0
    verified = _run('verify', '--manifest', str(manifest),
                    '--signature', str(sig), '--public', str(opub))
    assert verified.returncode == 1


def test_truncated_signature_fails_closed(tmp_path):
    priv, pub = _keypair(tmp_path)
    manifest = _manifest(tmp_path)
    sig = tmp_path / 'SHA256SUMS.sig'
    assert _run('sign', '--manifest', str(manifest),
                '--private', str(priv), '--out', str(sig)).returncode == 0
    sig.write_bytes(sig.read_bytes()[:32])  # truncate
    verified = _run('verify', '--manifest', str(manifest),
                    '--signature', str(sig), '--public', str(pub))
    assert verified.returncode == 1
    assert 'not the 64-byte' in verified.stderr


def test_missing_files_fail_closed(tmp_path):
    priv, pub = _keypair(tmp_path)
    manifest = _manifest(tmp_path)
    # No signature file at all.
    verified = _run('verify', '--manifest', str(manifest),
                    '--signature', str(tmp_path / 'absent.sig'),
                    '--public', str(pub))
    assert verified.returncode == 1
    assert 'cannot read signature' in verified.stderr
    # Empty manifest cannot be signed.
    empty = tmp_path / 'EMPTY'
    empty.write_bytes(b'')
    signed = _run('sign', '--manifest', str(empty),
                  '--private', str(priv), '--out', str(tmp_path / 'o.sig'))
    assert signed.returncode == 1


def test_malformed_private_key_fails_closed(tmp_path):
    priv, pub = _keypair(tmp_path)
    manifest = _manifest(tmp_path)
    priv.write_text('not a pem', encoding='utf-8')
    signed = _run('sign', '--manifest', str(manifest),
                  '--private', str(priv), '--out', str(tmp_path / 'o.sig'))
    assert signed.returncode == 1
    assert 'not a loadable PEM private key' in signed.stderr
