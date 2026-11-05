"""Sidebar folder workspace behavior against its real typed owner.

The classic chip picker no longer exists. Keep the pytest frontend lane on the
same focused Vitest specifications as TypeScript development, rather than
reconstructing an obsolete retained runtime or copying its picker logic.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests._jsdom import require_node_modules

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SPEC = "frontend/src/conversation/ui/folder-settings.test.ts"


def test_folder_workspace_editor_behaviors():
    require_node_modules('vitest')
    resolved = subprocess.run(
        ["node", "-p", 'require.resolve("vitest/package.json")'],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    vitest = Path(resolved.stdout.strip()).with_name("vitest.mjs")
    result = subprocess.run(
        ["node", str(vitest), "run", SPEC],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
