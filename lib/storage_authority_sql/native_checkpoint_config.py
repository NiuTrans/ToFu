"""Explicit pinned SQL checkpoint scope and byte budgets; no native loading.

The optional launch document is owned by the generated storage ABI contract.
SQL authority selection and ordinary semantic owners remain independent.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path

from lib.native_binary import NativeConfigurationError
from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC


@dataclass(frozen=True, slots=True)
class NativeCheckpointConfiguration:
    library_path: Path
    sha256: str
    binary_bytes: int
    tenant_id: int
    owner_user_id: int
    document_bytes: int
    identity_bytes: int

    def __post_init__(self):
        from tofu_db.generated_checkpoint_control import CHECKPOINT_DOCUMENT_MAX_BYTES
        if (not isinstance(self.library_path, Path) or not self.library_path.is_absolute()
                or not isinstance(self.sha256, str) or len(self.sha256) != 64
                or any(c not in '0123456789abcdef' for c in self.sha256)
                or type(self.binary_bytes) is not int or not 4 <= self.binary_bytes < 1 << 60
                or type(self.tenant_id) is not int or not 0 <= self.tenant_id < 1 << 64
                or type(self.owner_user_id) is not int or not 0 < self.owner_user_id < 1 << 64
                or type(self.document_bytes) is not int or not 0 < self.document_bytes <= CHECKPOINT_DOCUMENT_MAX_BYTES
                or type(self.identity_bytes) is not int or not 0 < self.identity_bytes <= self.document_bytes):
            raise NativeConfigurationError('invalid SQL checkpoint scope, image or budgets')

    @classmethod
    def from_environment(cls):
        policy = SPEC['application']
        raw = os.environ.get(policy['environment'], '')
        if not raw:
            return None
        try:
            if len(raw.encode()) > policy['configuration_bytes']:
                raise ValueError('configuration bound')
            value = json.loads(raw)
            if (not isinstance(value, dict) or set(value) != set(policy['configuration_fields'])
                    or value.pop('format') != policy['configuration_format']
                    or not isinstance(value['library_path'], str)):
                raise ValueError('configuration fields')
            return cls(**{**value, 'library_path': Path(value['library_path'])})
        except (ValueError, TypeError, UnicodeError, KeyError) as error:
            raise NativeConfigurationError('invalid explicit SQL checkpoint configuration') from error
