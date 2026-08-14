"""Safety and crash-consistency checks for the ffprobe bootstrap."""

from __future__ import annotations

import hashlib
import io
import os
from types import SimpleNamespace
import urllib.request
import zipfile

import pytest

pytestmark = pytest.mark.unit


class _Response:
    def __init__(self, body: bytes, declared: int | None = None):
        self._stream = io.BytesIO(body)
        self.headers = {
            'Content-Length': str(len(body) if declared is None else declared)
        }

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _zip(member=b'#!/bin/sh\n', *, symlink=False):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo('ffprobe')
        if symlink:
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
        zf.writestr(info, member)
    return out.getvalue()


def _wire(monkeypatch, tmp_path, body, *, run_ok=True):
    import lib.motion_video._env as env

    monkeypatch.setattr(env, 'ffprobe_bin', lambda: '')
    monkeypatch.setattr(env, 'media_shim_dir', lambda: str(tmp_path))
    monkeypatch.setattr(env, '_FFPROBE_ARCHIVE_SHA256',
                        hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(urllib.request, 'urlopen',
                        lambda *_args, **_kwargs: _Response(body))
    monkeypatch.setattr(env.subprocess, 'run', lambda *_args, **_kwargs:
                        SimpleNamespace(
                            returncode=0 if run_ok else 1,
                            stdout='ffprobe version 7.0.2' if run_ok else '',
                            stderr='' if run_ok else 'broken'))
    return env


def test_verified_binary_is_atomically_published(monkeypatch, tmp_path):
    binary = b'fake-static-ffprobe'
    archive = _zip(binary)
    env = _wire(monkeypatch, tmp_path, archive)

    result = env.ensure_ffprobe()

    assert result == str(tmp_path / 'ffprobe')
    assert (tmp_path / 'ffprobe').read_bytes() == binary
    assert os.access(result, os.X_OK)
    assert not list(tmp_path.glob('.ffprobe.*.part'))


def test_digest_mismatch_never_publishes(monkeypatch, tmp_path):
    archive = _zip()
    env = _wire(monkeypatch, tmp_path, archive)
    monkeypatch.setattr(env, '_FFPROBE_ARCHIVE_SHA256', '0' * 64)

    assert env.ensure_ffprobe() == ''
    assert not (tmp_path / 'ffprobe').exists()
    assert not list(tmp_path.glob('.ffprobe.*.part'))


def test_symlink_member_is_rejected(monkeypatch, tmp_path):
    archive = _zip(b'elsewhere', symlink=True)
    env = _wire(monkeypatch, tmp_path, archive)

    assert env.ensure_ffprobe() == ''
    assert not (tmp_path / 'ffprobe').exists()


def test_failed_executable_probe_leaves_no_partial(monkeypatch, tmp_path):
    archive = _zip()
    env = _wire(monkeypatch, tmp_path, archive, run_ok=False)

    assert env.ensure_ffprobe() == ''
    assert not (tmp_path / 'ffprobe').exists()
    assert not list(tmp_path.glob('.ffprobe.*.part'))


def test_declared_oversize_is_rejected_before_read(monkeypatch, tmp_path):
    import lib.motion_video._env as env

    archive = _zip()
    monkeypatch.setattr(env, 'ffprobe_bin', lambda: '')
    monkeypatch.setattr(env, 'media_shim_dir', lambda: str(tmp_path))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *_args, **_kwargs:
                        _Response(archive, env._FFPROBE_ARCHIVE_MAX_BYTES + 1))

    assert env.ensure_ffprobe() == ''
    assert not (tmp_path / 'ffprobe').exists()
