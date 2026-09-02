from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import PROJECT_ROOT


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_IGNORE_CONTENT = "*\n!.gitignore\n!.ignore\n"
_SEARCH_IGNORE_CONTENT = "*\n"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_run_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{os.urandom(3).hex()}"


def validate_run_id(value: str) -> str:
    if not _RUN_ID_RE.fullmatch(value):
        raise ValueError(
            "run id must be 1-96 characters and contain only letters, digits, '.', '_' or '-'"
        )
    return value


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _ensure_ignore_file(path: Path, required: str) -> None:
    if not path.exists():
        path.write_text(required, encoding="utf-8")
        return
    existing = path.read_text(encoding="utf-8", errors="replace")
    if existing.lstrip().startswith("*"):
        return
    with path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write("\n# Tofu evaluation artifacts: never index or commit.\n")
        handle.write(required)


def prepare_output_root(root: Path) -> Path:
    """Create an eval-owned root that is self-ignoring for Git and search tools."""
    root = root.expanduser().resolve()
    marker = root / ".tofu-swebench-eval-root"
    if root.exists() and not marker.exists():
        try:
            occupied = next(root.iterdir(), None) is not None
        except OSError as exc:
            raise ValueError(f"cannot inspect evaluation output root {root}: {exc}") from exc
        if occupied:
            raise ValueError(
                f"refusing to adopt non-empty directory as evaluation root: {root}"
            )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    _ensure_ignore_file(root / ".gitignore", _IGNORE_CONTENT)
    _ensure_ignore_file(root / ".ignore", _SEARCH_IGNORE_CONTENT)
    if not marker.exists():
        marker.write_text(
            json.dumps({"schema_version": 1, "created_at": utc_now()}) + "\n",
            encoding="utf-8",
        )
    return root


def create_run_dir(root: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    root = prepare_output_root(root)
    run_dir = root / run_id
    try:
        run_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise FileExistsError(
            f"run already exists: {run_dir}; use 'resume' or choose a new --run-id"
        ) from exc
    _ensure_ignore_file(run_dir / ".gitignore", _IGNORE_CONTENT)
    _ensure_ignore_file(run_dir / ".ignore", _SEARCH_IGNORE_CONTENT)
    return run_dir


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def harden_artifact_tree(root: Path) -> None:
    """Remove group/world permissions without following artifact symlinks."""

    root = root.expanduser().resolve(strict=True)
    if not root.is_dir() or root.stat().st_mode & 0o077:
        raise PermissionError(f"artifact root must already be private: {root}")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            path = current_path / name
            if path.is_symlink():
                continue
            try:
                mode = path.stat().st_mode & 0o7777
                path.chmod(mode & ~0o077)
            except FileNotFoundError:
                continue
def output_guard_status(root: Path, project_root: Path = PROJECT_ROOT) -> tuple[bool, str]:
    root = root.expanduser().resolve()
    marker = root / ".tofu-swebench-eval-root"
    if root.exists() and not marker.exists():
        try:
            occupied = next(root.iterdir(), None) is not None
        except OSError as exc:
            return False, f"cannot inspect output root {root}: {exc}"
        if occupied:
            return False, f"refusing to adopt non-empty unmarked directory: {root}"
    if not is_within(root, project_root):
        return True, f"outside project tree ({root})"
    try:
        relative = root.relative_to(project_root.resolve())
    except ValueError:
        return True, f"outside project tree ({root})"
    # The canonical in-repo emergency location is root-anchored in .gitignore.
    if relative.parts and relative.parts[0] in {".eval-runs", "eval-runs"}:
        return True, f"inside project but covered by root ignore ({relative})"
    return False, (
        f"{root} is inside the project and not under .eval-runs/ or eval-runs/; "
        "choose an external TOFU_EVAL_ROOT"
    )
