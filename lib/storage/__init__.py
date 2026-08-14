"""Process-isolated storage client API.

Application, worker, and plugin processes import this package.  Database
drivers and database paths deliberately live in :mod:`lib.storage_sidecar`.
"""

from lib.storage.client import StorageClient
from lib.storage.errors import StorageError, http_status_for_storage_error
from lib.storage.event_batcher import StorageEventBatcher
from lib.storage.manifest import ManifestError, validate_manifest
from lib.storage.runtime import StorageRuntime
from lib.storage.supervisor import StorageSupervisor
from lib.storage.service import (
    get_storage_client, start_storage, stop_storage, storage_status,
)

__all__ = [
    'ManifestError',
    'StorageClient',
    'StorageEventBatcher',
    'StorageError',
    'StorageRuntime',
    'StorageSupervisor',
    'get_storage_client',
    'start_storage',
    'stop_storage',
    'storage_status',
    'http_status_for_storage_error',
    'validate_manifest',
]
