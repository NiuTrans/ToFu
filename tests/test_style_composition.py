"""Executable contract for model-readable stylesheet ownership."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHEETS = (
    (ROOT / "frontend/src/styles/application", ROOT / "static/styles.css"),
    (ROOT / "frontend/src/styles/settings", ROOT / "static/settings.css"),
)
pytestmark = pytest.mark.unit


def test_style_manifests_are_safe_ordered_and_byte_authoritative() -> None:
    for source_root, output in SHEETS:
        manifest = json.loads(
            (source_root / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["version"] == 1
        assert ROOT / manifest["output"] == output
        assert manifest["sections"]

        paths: list[str] = []
        pieces: list[bytes] = []
        for row in manifest["sections"]:
            relative_path = row["path"]
            assert relative_path.endswith(".css")
            assert "\\" not in relative_path
            assert not Path(relative_path).is_absolute()
            assert ".." not in Path(relative_path).parts
            source_path = (source_root / relative_path).resolve()
            assert source_root.resolve() in source_path.parents
            assert source_path.stat().st_size <= 100 * 1024
            paths.append(relative_path)
            pieces.append(source_path.read_bytes())

        assert len(paths) == len(set(paths))
        assert b"".join(pieces) == output.read_bytes()


def test_generated_styles_are_fresh_hidden_and_built_from_sources() -> None:
    result = subprocess.run(
        ["node", "scripts/compose_frontend_styles.mjs", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    ignore = (ROOT / ".ignore").read_text(encoding="utf-8").splitlines()
    assert "static/styles.css" in ignore
    assert "static/settings.css" in ignore
    build = (ROOT / "scripts/build_frontend.mjs").read_text(encoding="utf-8")
    assert "composeStyles" in build
