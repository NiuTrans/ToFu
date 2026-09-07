"""Isolate storage before a test module is executed directly.

Standalone runners do not load pytest fixtures. Their first executable action
must call :func:`guard_standalone_storage`, which creates a disposable
personal-mode project root and starts the same Sidecar used by production.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import sys
import tempfile


_STANDALONE_ENVIRONMENT_NAMES = (
    'TOFU_STORAGE_PROJECT_ROOT',
    'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE',
    'TOFU_DEPLOYMENT_MODE',
    'TOFU_PROCESS_ROLE',
    'TOFU_DISTRIBUTED_PREVIEW_MODE',
    'TOFU_POSTGRES_DSN_FILE',
    'TOFU_REDIS_URL_FILE',
    'TOFU_REPLICA_ID',
)
_MISSING = object()


def _force_isolated_sidecar_environment() -> Path:
    root = Path(tempfile.mkdtemp(prefix='tofu-standalone-storage-')).resolve()
    os.environ['TOFU_STORAGE_PROJECT_ROOT'] = str(root)
    os.environ['TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE'] = '1'
    os.environ['TOFU_DEPLOYMENT_MODE'] = 'personal'
    os.environ['TOFU_PROCESS_ROLE'] = 'all'
    for name in (
        'TOFU_DISTRIBUTED_PREVIEW_MODE',
        'TOFU_POSTGRES_DSN_FILE', 'TOFU_REDIS_URL_FILE', 'TOFU_REPLICA_ID',
    ):
        os.environ.pop(name, None)
    return root


@contextmanager
def temporary_standalone_storage_environment():
    """Exercise standalone binding without leaking it into a pytest worker."""
    previous = {
        name: os.environ.get(name, _MISSING)
        for name in _STANDALONE_ENVIRONMENT_NAMES
    }
    root = _force_isolated_sidecar_environment()
    try:
        yield root
    finally:
        for name, value in previous.items():
            if value is _MISSING:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(root, ignore_errors=True)


def guard_standalone_storage(
    context: str = 'standalone',
    *,
    start_authority: bool = True,
) -> None:
    """Bind a direct test runner to disposable storage and verify the bind."""
    _force_isolated_sidecar_environment()
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

    try:
        from tests.conftest import _assert_isolated_storage
    except Exception:
        from conftest import _assert_isolated_storage  # type: ignore
    _assert_isolated_storage(context)

    if start_authority:
        from lib.storage import start_storage
        start_storage()


__all__ = [
    'guard_standalone_storage',
    'temporary_standalone_storage_environment',
]
