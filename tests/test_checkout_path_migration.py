"""Checkout relocation updates only declared bounded machine-local state."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate_checkout_path.py"


def _run(old_root: Path, new_root: Path, *extra: str):
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--old-root", str(old_root),
            "--new-root", str(new_root),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, json.loads(result.stdout)


def test_dry_run_then_apply_updates_declared_state_only(tmp_path):
    old_root = tmp_path / "chatui"
    new_root = tmp_path / "tofu"
    (new_root / ".git" / "worktrees" / "w").mkdir(parents=True)
    (new_root / ".tofu" / "memories").mkdir(parents=True)
    (new_root / "data" / "config").mkdir(parents=True)
    (new_root / "data" / "integration" / "worktrees" / "w").mkdir(
        parents=True
    )
    (new_root / "logs").mkdir()

    state_files = [
        new_root / ".tofu_env.json",
        new_root / ".git" / "worktrees" / "w" / "gitdir",
        new_root / ".tofu" / "memories" / "project.md",
        new_root / "data" / "config" / "mcp_servers.json",
        new_root / "data" / "integration" / "worktrees" / "w" / ".git",
    ]
    for path in state_files:
        path.write_text(f"root={old_root}\n", encoding="utf-8")
    historical_log = new_root / "logs" / "app.log"
    historical_log.write_text(f"started from {old_root}\n", encoding="utf-8")

    dry_result, dry = _run(old_root, new_root)
    assert dry_result.returncode == 0, dry_result.stderr
    assert dry["mode"] == "dry-run"
    assert dry["changed_files"] == len(state_files)
    assert all(str(old_root) in path.read_text() for path in state_files)

    apply_result, applied = _run(old_root, new_root, "--apply")
    assert apply_result.returncode == 0, apply_result.stderr
    assert applied["replacements"] == len(state_files)
    assert all(str(new_root) in path.read_text() for path in state_files)
    assert str(old_root) in historical_log.read_text()
    assert (new_root / applied["receipt"]).is_file()


def test_binary_state_is_reported_and_left_unchanged(tmp_path):
    old_root = tmp_path / "chatui"
    new_root = tmp_path / "tofu"
    (new_root / ".git").mkdir(parents=True)
    binary = new_root / ".git" / "binary"
    original = b"\0" + str(old_root).encode()
    binary.write_bytes(original)
    result, summary = _run(old_root, new_root, "--apply")
    assert result.returncode == 0
    assert summary["skipped_binary_files"] == 0
    # .git objects/config are outside the declared .git/worktrees owner.
    assert binary.read_bytes() == original
