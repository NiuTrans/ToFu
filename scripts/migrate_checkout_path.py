#!/usr/bin/env python3
"""Relink bounded machine-local state after moving a Tofu checkout.

Dry-run is the default. The script replaces only the exact old absolute root
inside declared text-state owners and linked-worktree metadata. It never scans
databases, logs, uploads, Git objects, dependency caches, or user transcripts.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
import time
from pathlib import Path


STATE_OWNERS = (
    ".tofu_env.json",
    ".tofu",
    ".chatui",
    "data/config",
    "data/memories",
    "data/desktop_dist",
    "data/integration/worktrees",
    ".git/worktrees",
)
PRUNED_DIRECTORIES = {
    ".cache", ".pytest_cache", ".ruff_cache", ".tofu", ".chatui",
    ".venv", "build", "codex", "dist", "logs", "node_modules",
}
MAX_FILES = 100_000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_CHANGED_FILES = 10_000
PREVIEW_PATHS = 20


def _absolute(path_text: str, *, must_exist: bool) -> Path:
    path = Path(path_text).expanduser().resolve(strict=must_exist)
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError(f"unsafe checkout root: {path}")
    return path


def _candidate_files(new_root: Path):
    visited = 0
    seen: set[Path] = set()
    for relative_owner in STATE_OWNERS:
        owner = new_root / relative_owner
        if owner.is_symlink() or not owner.exists():
            continue
        if owner.is_file():
            candidates = (owner,)
        else:
            def _walk():
                for directory, names, files in os.walk(owner, followlinks=False):
                    names[:] = [
                        name for name in names
                        if name not in PRUNED_DIRECTORIES
                        and not (Path(directory) / name).is_symlink()
                    ]
                    for name in files:
                        yield Path(directory) / name

            candidates = _walk()
        for path in candidates:
            visited += 1
            if visited > MAX_FILES:
                raise RuntimeError(f"state scan exceeds {MAX_FILES} files")
            if path in seen or path.is_symlink() or not path.is_file():
                continue
            seen.add(path)
            yield path


def _replace_atomically(path: Path, old_bytes: bytes, new_bytes: bytes) -> int:
    original = path.read_bytes()
    occurrences = original.count(old_bytes)
    if not occurrences:
        return 0
    mode = stat.S_IMODE(path.stat().st_mode)
    replacement = original.replace(old_bytes, new_bytes)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".tofu-path-migration-",
            delete=False,
        ) as temporary:
            temporary.write(replacement)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return occurrences


def migrate(old_root: Path, new_root: Path, *, apply: bool) -> dict:
    old_bytes = os.fsencode(str(old_root))
    new_bytes = os.fsencode(str(new_root))
    changed: list[Path] = []
    replacements = 0
    scanned = 0
    skipped_large = 0
    skipped_binary = 0

    for path in _candidate_files(new_root):
        scanned += 1
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            skipped_large += 1
            continue
        content = path.read_bytes()
        if old_bytes not in content:
            continue
        if b"\0" in content[:4096]:
            skipped_binary += 1
            continue
        changed.append(path)
        if len(changed) > MAX_CHANGED_FILES:
            raise RuntimeError(
                f"path migration exceeds {MAX_CHANGED_FILES} changed files"
            )
        occurrences = content.count(old_bytes)
        if apply:
            occurrences = _replace_atomically(path, old_bytes, new_bytes)
        replacements += occurrences

    summary = {
        "format": "tofu.checkout-path-migration/v1",
        "mode": "apply" if apply else "dry-run",
        "old_root": str(old_root),
        "new_root": str(new_root),
        "scanned_files": scanned,
        "changed_files": len(changed),
        "replacements": replacements,
        "skipped_large_files": skipped_large,
        "skipped_binary_files": skipped_binary,
        "changed_preview": [
            path.relative_to(new_root).as_posix()
            for path in changed[:PREVIEW_PATHS]
        ],
    }
    if apply:
        receipt_directory = (
            new_root / "data" / "config" / "checkout_path_migrations"
        )
        receipt_directory.mkdir(parents=True, exist_ok=True)
        receipt = receipt_directory / f"{int(time.time())}.json"
        receipt.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary["receipt"] = receipt.relative_to(new_root).as_posix()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-root", required=True)
    parser.add_argument("--new-root", required=True)
    parser.add_argument(
        "--apply", action="store_true",
        help="atomically replace declared state references (default: dry-run)",
    )
    args = parser.parse_args()
    try:
        old_root = _absolute(args.old_root, must_exist=False)
        new_root = _absolute(args.new_root, must_exist=True)
        if old_root == new_root:
            raise ValueError("old and new checkout roots are identical")
        if not (new_root / ".git").exists():
            raise ValueError(f"new root is not a Git checkout: {new_root}")
        summary = migrate(old_root, new_root, apply=args.apply)
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
