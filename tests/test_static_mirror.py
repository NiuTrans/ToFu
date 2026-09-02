"""Budgets and fallback semantics for the reconstructible Vite mirror."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lib.static_mirror import StaticViteMirror


pytestmark = pytest.mark.unit


def _static_tree(tmp_path: Path) -> Path:
    static_dir = tmp_path / "source" / "static"
    (static_dir / "vite" / "assets").mkdir(parents=True)
    (static_dir / "vite" / "assets" / "app.js").write_text(
        "export const ready = true;", encoding="utf-8")
    (static_dir / "vite" / "manifest.json").write_text(
        '{"app":"assets/app.js"}', encoding="utf-8")
    (static_dir / "favicon.ico").write_bytes(b"source-only")
    return static_dir


def test_vite_mirror_is_atomic_scoped_and_reusable(tmp_path):
    static_dir = _static_tree(tmp_path)
    mirror = StaticViteMirror(
        str(static_dir),
        cache_root=str(tmp_path / "local-cache"),
        reserve_bytes=0,
    )

    first = mirror.prepare()

    assert first.active is True
    assert first.reason == "ready"
    assert first.file_count == 2
    assert first.total_bytes > 0
    assert mirror.static_dir_for("vite/assets/app.js") == first.static_dir
    assert mirror.static_dir_for("favicon.ico") == str(static_dir)
    assert (Path(first.static_dir) / "vite/assets/app.js").read_text(
        encoding="utf-8") == "export const ready = true;"

    second = mirror.prepare()
    assert second == first
    assert not list((tmp_path / "local-cache").rglob(".building-*"))


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "expected_reason"),
    [
        ("max_bytes", 5, "byte budget"),
        ("max_files", 1, "file-count budget"),
        ("max_file_bytes", 5, "oversized file"),
    ],
)
def test_vite_mirror_falls_back_when_a_budget_is_exceeded(
    tmp_path, limit_name, limit_value, expected_reason,
):
    static_dir = _static_tree(tmp_path)
    options = {limit_name: limit_value}
    mirror = StaticViteMirror(
        str(static_dir),
        cache_root=str(tmp_path / "local-cache"),
        reserve_bytes=0,
        **options,
    )

    status = mirror.prepare()

    assert status.active is False
    assert expected_reason in status.reason
    assert status.static_dir == str(static_dir)
    assert mirror.static_dir_for("vite/assets/app.js") == str(static_dir)


def test_vite_mirror_rejects_symlinks(tmp_path):
    static_dir = _static_tree(tmp_path)
    os.symlink(
        static_dir / "favicon.ico",
        static_dir / "vite" / "assets" / "unsafe.js",
    )
    mirror = StaticViteMirror(
        str(static_dir),
        cache_root=str(tmp_path / "local-cache"),
        reserve_bytes=0,
    )

    status = mirror.prepare()

    assert status.active is False
    assert "non-regular file" in status.reason


def test_vite_mirror_retains_at_most_three_bounded_generations(tmp_path):
    static_dir = _static_tree(tmp_path)
    mirror = StaticViteMirror(
        str(static_dir),
        cache_root=str(tmp_path / "local-cache"),
        reserve_bytes=0,
    )
    asset = static_dir / "vite" / "assets" / "app.js"

    for generation in range(6):
        payload = f"export const generation = {generation};"
        asset.write_text(payload, encoding="utf-8")
        timestamp_ns = 2_000_000_000_000_000_000 + generation
        os.utime(asset, ns=(timestamp_ns, timestamp_ns))
        assert mirror.prepare().active is True

    generations = list((tmp_path / "local-cache").rglob("generation-*"))
    assert len(generations) <= 3
    assert sum(
        path.stat().st_size
        for generation in generations
        for path in generation.rglob("*")
        if path.is_file()
    ) <= 3 * mirror.max_bytes
