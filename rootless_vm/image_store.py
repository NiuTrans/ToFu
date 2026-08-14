"""Digest-pinned per-task payload lookup for multi-task Harbor jobs."""

from __future__ import annotations

import hashlib
import functools
import json
import os
from pathlib import Path


_SCHEMA = 1


def _checked_private_dir(value: str | os.PathLike[str], label: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link: {candidate}")
    path = candidate.resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"{label} must be a directory: {path}")
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"{label} must not be group/world accessible: {path}")
    return path


def _verify_sha256(path: Path, expected: str, label: str) -> None:
    normalized = expected.removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError(f"{label} SHA-256 must contain exactly 64 hexadecimal digits")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != normalized:
        raise ValueError(f"{label} SHA-256 mismatch")


def resolve_image_store(
    store_value: str | os.PathLike[str], image_reference: str
) -> tuple[Path, str, str]:
    """Resolve one immutable payload without trusting task-controlled paths."""

    store = _checked_private_dir(store_value, "image_store")
    index_path = store / "index.json"
    if index_path.is_symlink() or not index_path.is_file():
        raise ValueError(f"image_store index must be a regular file: {index_path}")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("image_store index is not valid JSON") from exc
    if not isinstance(index, dict) or index.get("schema") != _SCHEMA:
        raise ValueError("image_store index has an unsupported schema")
    images = index.get("images")
    if not isinstance(images, dict):
        raise ValueError("image_store index has no image map")
    entry = images.get(image_reference)
    if not isinstance(entry, dict):
        raise ValueError(f"image_store has no payload for {image_reference!r}")
    relative = entry.get("iso")
    expected_sha256 = entry.get("sha256")
    loaded_reference = entry.get("loaded_image_reference")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("image_store ISO path must be a non-empty relative path")
    if ".." in Path(relative).parts:
        raise ValueError("image_store ISO path must not contain '..'")
    if not isinstance(expected_sha256, str):
        raise ValueError("image_store ISO is missing its SHA-256")
    if not isinstance(loaded_reference, str) or not loaded_reference.startswith(
        "sha256:"
    ):
        raise ValueError("image_store payload is missing its loaded image reference")
    candidate = store / relative
    if candidate.is_symlink():
        raise ValueError("image_store ISO must not be a symbolic link")
    iso = candidate.resolve(strict=True)
    if store not in iso.parents or not iso.is_file():
        raise ValueError("image_store ISO escapes its private root")
    stat = iso.stat()
    _verify_sha256_cached(
        str(iso),
        expected_sha256,
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
    )
    return iso, expected_sha256, loaded_reference


@functools.lru_cache(maxsize=256)
def _verify_sha256_cached(
    path: str,
    expected: str,
    _device: int,
    _inode: int,
    _size: int,
    _mtime_ns: int,
) -> None:
    """Hash once per immutable file identity while detecting ordinary replacement."""

    _verify_sha256(Path(path), expected, "image_store ISO")
