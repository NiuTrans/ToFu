"""A failed mandatory cleanup must stop sharing before copy or publication."""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

export = pytest.importorskip('export', reason='maintainer tooling is not distributed')
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['internal', 'opensource'])
@pytest.mark.parametrize('name', ['export_pkg', 'export.py', '.tofu_env.json'])
def test_failed_mandatory_cleanup_aborts_export(tmp_path, monkeypatch, mode, name):
    import export_pkg._orchestrate as orchestrate

    source = tmp_path / 'source'
    source.mkdir()
    destination = tmp_path / 'destination'
    destination.mkdir()
    stale = destination / name
    if name == 'export_pkg':
        stale.mkdir()
        (stale / '_lists.py').write_text('private-marker-placeholder')
    else:
        stale.write_text('{}')
    monkeypatch.setattr(orchestrate, 'ROOT', source)
    monkeypatch.setattr(orchestrate, '_have_tool', lambda tool: tool == 'rm')
    attempts = []

    def fail_removal(command, **kwargs):
        attempts.append(command)
        assert command[:3] == ['rm', '-rf', '--']
        return subprocess.CompletedProcess(command, 1)

    def forbid_copy(*args, **kwargs):
        pytest.fail('copy started after mandatory cleanup failed')

    monkeypatch.setattr(orchestrate.subprocess, 'run', fail_removal)
    monkeypatch.setattr(orchestrate, '_export_via_tar_with_sanitize', forbid_copy)
    with pytest.raises(export.ExportIntegrityError, match='mandatory cleanup'):
        orchestrate.export_project(mode, Path(destination))
    assert attempts
    assert stale.exists(), 'failure injection must leave the unsafe path in place'
