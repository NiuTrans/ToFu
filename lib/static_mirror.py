"""Bounded local mirror for reconstructible Vite delivery artifacts.

The repository can live on a network/FUSE volume while HTTP requests arrive
through a latency-sensitive reverse proxy.  This module copies only the
reconstructible ``static/vite`` tree to host-local temporary storage during the
production build phase.  It never mirrors durable user data and every failure
falls back to the authoritative source tree.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
import uuid


_MIB = 1024 * 1024
_DEFAULT_MAX_BYTES = 64 * _MIB
_HARD_MAX_BYTES = 128 * _MIB
_DEFAULT_MAX_FILES = 4096
_HARD_MAX_FILES = 16_384
_DEFAULT_MAX_FILE_BYTES = 16 * _MIB
_HARD_MAX_FILE_BYTES = 32 * _MIB
_DEFAULT_RESERVE_BYTES = 256 * _MIB
_RETAIN_GENERATIONS = 3


@dataclass(frozen=True)
class StaticMirrorStatus:
    """Observable result of one mirror preparation attempt."""

    active: bool
    reason: str
    static_dir: str
    file_count: int = 0
    total_bytes: int = 0


@dataclass(frozen=True)
class _SourceFile:
    relative_path: str
    size: int
    mtime_ns: int


def _bounded_environment_integer(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


class StaticViteMirror:
    """Prepare and resolve one bounded ``static/vite`` local mirror."""

    def __init__(
        self,
        source_static_dir: str,
        *,
        cache_root: str | None = None,
        enabled: bool = True,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_files: int = _DEFAULT_MAX_FILES,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        reserve_bytes: int = _DEFAULT_RESERVE_BYTES,
    ) -> None:
        self.source_static_dir = os.path.abspath(source_static_dir)
        default_root = os.path.join(
            tempfile.gettempdir(), f"tofu-static-mirror-{os.getuid()}")
        self.cache_root = os.path.abspath(cache_root or default_root)
        self.enabled = bool(enabled)
        self.max_bytes = min(_HARD_MAX_BYTES, max(1, int(max_bytes)))
        self.max_files = min(_HARD_MAX_FILES, max(1, int(max_files)))
        self.max_file_bytes = min(
            _HARD_MAX_FILE_BYTES, max(1, int(max_file_bytes)))
        self.reserve_bytes = max(0, int(reserve_bytes))
        self._active_static_dir: str | None = None

    @classmethod
    def from_environment(cls, source_static_dir: str) -> "StaticViteMirror":
        enabled = os.environ.get("TOFU_STATIC_MIRROR", "1").strip().lower()
        return cls(
            source_static_dir,
            cache_root=os.environ.get("TOFU_STATIC_MIRROR_DIR") or None,
            enabled=enabled not in {"0", "false", "no", "off"},
            max_bytes=_bounded_environment_integer(
                "TOFU_STATIC_MIRROR_MAX_BYTES",
                _DEFAULT_MAX_BYTES,
                minimum=_MIB,
                maximum=_HARD_MAX_BYTES,
            ),
            max_files=_bounded_environment_integer(
                "TOFU_STATIC_MIRROR_MAX_FILES",
                _DEFAULT_MAX_FILES,
                minimum=1,
                maximum=_HARD_MAX_FILES,
            ),
            max_file_bytes=_bounded_environment_integer(
                "TOFU_STATIC_MIRROR_MAX_FILE_BYTES",
                _DEFAULT_MAX_FILE_BYTES,
                minimum=_MIB,
                maximum=_HARD_MAX_FILE_BYTES,
            ),
            reserve_bytes=_bounded_environment_integer(
                "TOFU_STATIC_MIRROR_RESERVE_BYTES",
                _DEFAULT_RESERVE_BYTES,
                minimum=0,
                maximum=16 * 1024 * _MIB,
            ),
        )

    @property
    def active_static_dir(self) -> str | None:
        return self._active_static_dir

    def static_dir_for(self, filename: str) -> str:
        """Return the local root only for paths owned by the Vite mirror."""
        if (
            self._active_static_dir
            and (filename == "vite" or filename.startswith("vite/"))
        ):
            return self._active_static_dir
        return self.source_static_dir

    def prepare(self) -> StaticMirrorStatus:
        """Atomically prepare a local generation, or report source fallback."""
        self._active_static_dir = None
        if not self.enabled:
            return self._fallback("disabled")

        source_vite = Path(self.source_static_dir) / "vite"
        try:
            files, total_bytes, generation = self._scan_source(source_vite)
            source_key = hashlib.sha256(
                self.source_static_dir.encode("utf-8", errors="surrogatepass")
            ).hexdigest()[:16]
            cache_root = Path(self.cache_root)
            cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._ensure_private_directory(cache_root)
            owner_root = Path(self.cache_root) / f"source-{source_key}"
            owner_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._ensure_private_directory(owner_root)
            self._prune_abandoned_builds(owner_root)
            target = owner_root / f"generation-{generation}"

            if not self._is_complete(target, generation, files, total_bytes):
                if target.exists() or target.is_symlink():
                    if (
                        target.parent != owner_root
                        or target.is_symlink()
                        or not target.name.startswith("generation-")
                    ):
                        raise ValueError("unsafe static mirror generation target")
                    shutil.rmtree(target)
                available = shutil.disk_usage(owner_root).free
                if available - total_bytes < self.reserve_bytes:
                    return self._fallback("insufficient_local_disk")
                self._copy_generation(
                    source_vite,
                    owner_root,
                    target,
                    generation,
                    files,
                    total_bytes,
                )

            self._active_static_dir = str(target)
            self._prune_old_generations(owner_root, target)
            return StaticMirrorStatus(
                active=True,
                reason="ready",
                static_dir=str(target),
                file_count=len(files),
                total_bytes=total_bytes,
            )
        except (OSError, ValueError) as exc:
            return self._fallback(
                f"{type(exc).__name__}: {str(exc)[:160]}")

    def _fallback(self, reason: str) -> StaticMirrorStatus:
        return StaticMirrorStatus(
            active=False,
            reason=reason,
            static_dir=self.source_static_dir,
        )

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        metadata = os.lstat(path)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError("static mirror cache directory is not privately owned")
        os.chmod(path, 0o700)

    @staticmethod
    def _prune_abandoned_builds(owner_root: Path) -> None:
        cutoff = time.time() - 600
        for child in owner_root.iterdir():
            try:
                metadata = child.lstat()
            except OSError:
                continue
            if (
                child.parent == owner_root
                and child.name.startswith(".building-")
                and stat.S_ISDIR(metadata.st_mode)
                and metadata.st_mtime < cutoff
            ):
                shutil.rmtree(child, ignore_errors=True)

    def _scan_source(
        self, source_vite: Path,
    ) -> tuple[list[_SourceFile], int, str]:
        if not source_vite.is_dir() or source_vite.is_symlink():
            raise ValueError("static/vite is missing or unsafe")
        files: list[_SourceFile] = []
        total_bytes = 0
        digest = hashlib.sha256()
        for directory, directory_names, file_names in os.walk(source_vite):
            directory_names.sort()
            file_names.sort()
            directory_path = Path(directory)
            if directory_path.is_symlink():
                raise ValueError("static/vite contains a directory symlink")
            for name in directory_names:
                if (directory_path / name).is_symlink():
                    raise ValueError("static/vite contains a directory symlink")
            for name in file_names:
                path = directory_path / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError("static/vite contains a non-regular file")
                stat = path.stat()
                if stat.st_size > self.max_file_bytes:
                    raise ValueError("static/vite contains an oversized file")
                relative = path.relative_to(source_vite).as_posix()
                files.append(_SourceFile(relative, stat.st_size, stat.st_mtime_ns))
                total_bytes += stat.st_size
                if len(files) > self.max_files:
                    raise ValueError("static/vite exceeds the file-count budget")
                if total_bytes > self.max_bytes:
                    raise ValueError("static/vite exceeds the byte budget")
                digest.update(relative.encode("utf-8", errors="surrogatepass"))
                digest.update(b"\0")
                digest.update(str(stat.st_size).encode("ascii"))
                digest.update(b"\0")
                digest.update(str(stat.st_mtime_ns).encode("ascii"))
                digest.update(b"\0")
        if not files:
            raise ValueError("static/vite is empty")
        return files, total_bytes, digest.hexdigest()[:20]

    @staticmethod
    def _marker_payload(
        generation: str, files: list[_SourceFile], total_bytes: int,
    ) -> dict[str, int | str]:
        return {
            "version": 1,
            "generation": generation,
            "file_count": len(files),
            "total_bytes": total_bytes,
        }

    def _is_complete(
        self,
        target: Path,
        generation: str,
        files: list[_SourceFile],
        total_bytes: int,
    ) -> bool:
        try:
            marker = json.loads(
                (target / ".tofu-static-mirror.json").read_text("utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        return marker == self._marker_payload(generation, files, total_bytes)

    def _copy_generation(
        self,
        source_vite: Path,
        owner_root: Path,
        target: Path,
        generation: str,
        files: list[_SourceFile],
        total_bytes: int,
    ) -> None:
        temporary = owner_root / f".building-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            (temporary / "vite").mkdir(mode=0o700, parents=True)
            for item in files:
                source = source_vite / item.relative_path
                destination = temporary / "vite" / item.relative_path
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with source.open("rb") as source_handle, destination.open("xb") as output:
                    shutil.copyfileobj(source_handle, output, length=1024 * 1024)
                copied = destination.stat()
                if copied.st_size != item.size:
                    raise OSError("static/vite changed while it was mirrored")
                source_after_copy = source.stat()
                if (
                    source_after_copy.st_size != item.size
                    or source_after_copy.st_mtime_ns != item.mtime_ns
                ):
                    raise OSError("static/vite changed while it was mirrored")
                os.utime(destination, ns=(item.mtime_ns, item.mtime_ns))
            marker = self._marker_payload(generation, files, total_bytes)
            (temporary / ".tofu-static-mirror.json").write_text(
                json.dumps(marker, sort_keys=True), encoding="utf-8")
            try:
                os.replace(temporary, target)
            except OSError:
                if not self._is_complete(target, generation, files, total_bytes):
                    raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _prune_old_generations(owner_root: Path, active: Path) -> None:
        generations = sorted(
            (
                child for child in owner_root.iterdir()
                if child.is_dir()
                and not child.is_symlink()
                and child.name.startswith("generation-")
            ),
            key=lambda child: child.stat().st_mtime_ns,
            reverse=True,
        )
        retained = {active}
        for generation in generations:
            if len(retained) >= _RETAIN_GENERATIONS:
                break
            retained.add(generation)
        for child in generations:
            if child not in retained and child.parent == owner_root:
                shutil.rmtree(child, ignore_errors=True)


__all__ = ["StaticMirrorStatus", "StaticViteMirror"]
