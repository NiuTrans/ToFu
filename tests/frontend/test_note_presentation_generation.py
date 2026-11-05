"""Both note presentation outputs follow the registry; drift checks are read-only."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ('gen_system_note_presentation.mjs', 'check_note_presentation_parity.mjs')


def test_registry_change_regenerates_both_surfaces_and_checks_without_writes(tmp_path):
    if not shutil.which('node'):
        pytest.skip('node is required for frontend generators')
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    for name in SCRIPTS:
        shutil.copyfile(ROOT / 'scripts' / name, scripts / name)
    package = tmp_path / 'lib/tasks_pkg/context_composer'
    package.mkdir(parents=True)
    for part in (tmp_path / 'lib', tmp_path / 'lib/tasks_pkg', package):
        (part / '__init__.py').write_text('')
    registry = package / 'registry.py'

    def set_registry(reason):
        registry.write_text(
            'from types import SimpleNamespace\n'
            'def note_presentations():\n'
            f'    return {{{reason!r}: SimpleNamespace(label_key="note.incoming", tone="info", fallback="Incoming", glyph="info")}}\n'
            'def note_display_policies():\n'
            f'    return {{{reason!r}: {{"noteKind": "context-injection", "groupContext": False, "provenanceField": ""}}}}\n'
        )

    runtime = tmp_path / 'frontend/src/runtime/sections/ui/tool_rounds.js'
    runtime.parent.mkdir(parents=True)
    before = '// unrelated prefix\n/* BEGIN GENERATED (registry note_presentations) */\n'
    after = '\n/* END GENERATED */\n// unrelated suffix\n'
    runtime.write_text(before + 'const _SEG_NOTE_REASON_CHIP = {};' + after)
    typed = tmp_path / 'frontend/src/conversation/ui/system-note-presentation.generated.ts'

    def invoke(name, mode):
        return subprocess.run(['node', str(scripts / name), mode], cwd=tmp_path,
                              text=True, capture_output=True, timeout=30)

    set_registry('inbox_delivery')
    for name in SCRIPTS:
        result = invoke(name, '--check')
        assert result.returncode != 0
        result = invoke(name, '--write')
        assert result.returncode == 0, result.stderr
        result = invoke(name, '--check')
        assert result.returncode == 0, result.stderr
    assert 'inbox_delivery' in typed.read_text()
    assert 'inbox_delivery' in runtime.read_text()
    assert '"groupContext": false' in typed.read_text()
    outputs = {path: path.read_bytes() for path in (typed, runtime)}
    set_registry('new_runtime_input')
    for name in SCRIPTS:
        assert invoke(name, '--check').returncode != 0
    assert all(path.read_bytes() == data for path, data in outputs.items())
    for name in SCRIPTS:
        result = invoke(name, '--write')
        assert result.returncode == 0, result.stderr
        assert invoke(name, '--check').returncode == 0
    for path in (typed, runtime):
        assert 'new_runtime_input' in path.read_text()
        assert 'inbox_delivery' not in path.read_text()
    assert runtime.read_text().startswith(before)
    assert runtime.read_text().endswith(after)
