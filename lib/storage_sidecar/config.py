"""Fail-closed sidecar configuration; paths are derived inside the process."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, '')
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'invalid {name}') from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f'{name} must be between {minimum} and {maximum}')
    return value


@dataclass(frozen=True, slots=True)
class SidecarConfig:
    project_root: Path
    data_dir: Path
    logs_dir: Path
    backend: str
    token: str
    sqlite_path: Path
    pgdata: Path
    read_pool_size: int
    write_pool_size: int
    acquire_timeout_s: float = 2.0
    transaction_timeout_s: float = 5.0
    idle_lifetime_s: float = 60.0
    max_lifetime_s: float = 900.0

    @classmethod
    def from_environment(cls) -> 'SidecarConfig':
        backend = (os.environ.get('TOFU_DB_BACKEND') or 'sqlite').strip().lower()
        if backend not in {'sqlite', 'postgres'}:
            raise RuntimeError('TOFU_DB_BACKEND must be exactly sqlite or postgres')
        token = os.environ.get('TOFU_STORAGE_TOKEN', '')
        if len(token) < 32:
            raise RuntimeError('TOFU_STORAGE_TOKEN is missing or too short')

        source_root = Path(__file__).resolve().parents[2]
        override = os.environ.get('TOFU_STORAGE_PROJECT_ROOT', '').strip()
        if override:
            if os.environ.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') != '1':
                raise RuntimeError('project-root override requires explicit test authority')
            source_root = Path(override).resolve()
        project_root = source_root.resolve()
        data_dir = (project_root / 'data').resolve()
        logs_dir = (project_root / 'logs').resolve()
        for path in (data_dir, logs_dir):
            try:
                path.relative_to(project_root)
            except ValueError as exc:
                raise RuntimeError('persistent storage escaped the project root') from exc
            path.mkdir(parents=True, exist_ok=True)

        if backend == 'sqlite':
            read_pool_size = _bounded_int('TOFU_STORAGE_SQLITE_READ_POOL', 16, 1, 64)
            write_pool_size = 1
        else:
            read_pool_size = _bounded_int('TOFU_STORAGE_PG_READ_POOL', 32, 1, 256)
            write_pool_size = _bounded_int('TOFU_STORAGE_PG_WRITE_POOL', 16, 1, 128)
        return cls(
            project_root=project_root,
            data_dir=data_dir,
            logs_dir=logs_dir,
            backend=backend,
            token=token,
            sqlite_path=data_dir / 'tofu.db',
            pgdata=data_dir / 'pgdata',
            read_pool_size=read_pool_size,
            write_pool_size=write_pool_size,
        )


__all__ = ['SidecarConfig']
