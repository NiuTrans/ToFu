"""Sidebar folder workspace behavior against its real typed owner.

The classic chip picker no longer exists. Keep the pytest frontend lane on the
same focused Vitest specifications as TypeScript development, rather than
reconstructing an obsolete retained runtime or copying its picker logic.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SPEC = "frontend/src/conversation/ui/folder-settings.test.ts"


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_folder_workspace_editor_behaviors():
    resolved = subprocess.run(
        ["node", "-p", 'require.resolve("vitest/package.json")'],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    if resolved.returncode:
        pytest.skip("Vitest dev dependencies are required")
    vitest = Path(resolved.stdout.strip()).with_name("vitest.mjs")
    result = subprocess.run(
        ["node", str(vitest), "run", SPEC],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
