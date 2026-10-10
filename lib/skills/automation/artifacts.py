"""Private complete skill artifacts, durably sealed before storage admission.

Entry points: SkillArtifactStore.promote/verified_root. Deployment supplies the
private root and launch-derived byte/headroom budgets; no project path or remote
input selects a storage directory. Content is immutable and deduplicated within
an explicit owner/tenant scope. This layer never deletes accepted artifacts or
executes bundled code; the selected database owns acceptance and quota ledgers.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from lib.automations.errors import AutomationError
from lib.json_store import locked_path
from lib.skills.installer import InstallerError, validated_skill_package_seal

from .source_records import credential_scope


def _directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise AutomationError('skills_artifact_storage', 'Skill artifact directory is not private')


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SkillArtifactStore:
    def __init__(self, root, *, maximum_package_bytes, reserved_free_bytes):
        self.root = Path(root)
        if not self.root.is_absolute() or self.root != self.root.resolve():
            raise ValueError('Artifact root must be an absolute canonical deployment path')
        if type(maximum_package_bytes) is not int or maximum_package_bytes <= 0:
            raise ValueError('Artifact byte budget must be explicit and positive')
        if type(reserved_free_bytes) is not int or reserved_free_bytes < 0:
            raise ValueError('Artifact free-space reservation must be explicit')
        _directory(self.root, create=True)
        _sync_directory(self.root.parent)
        self.maximum_package_bytes = maximum_package_bytes
        self.reserved_free_bytes = reserved_free_bytes

    def _scope(self, principal, *, create=False):
        owner = principal.require_owner(context='frozen skill artifact')
        scope = self.root / credential_scope(owner, principal.tenant_id)
        _directory(scope, create=create)
        if create:
            _sync_directory(self.root)
        return owner, scope

    def _read(self, principal, digest):
        owner, scope = self._scope(principal)
        if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
            raise AutomationError('skills_artifact_integrity', 'Invalid frozen content identity')
        directory = scope / digest
        _directory(directory)
        _directory(directory / 'content')
        manifest_path = directory / 'seal.json'
        info = manifest_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 8 * 1024 * 1024):
            raise AutomationError('skills_artifact_integrity', 'Frozen manifest is unavailable')
        try:
            descriptor = json.loads(manifest_path.read_text())
            seal = validated_skill_package_seal(str(directory / 'content'))
        except (OSError, ValueError, TypeError, InstallerError) as exc:
            raise AutomationError('skills_artifact_integrity', 'Frozen package verification failed') from exc
        expected = {'contract': 'tofu.skill-artifact/v1', 'ownerId': owner,
                    'tenantId': principal.tenant_id, 'seal': seal}
        if descriptor != expected or seal['content_sha256'] != digest:
            raise AutomationError('skills_artifact_integrity', 'Frozen package bytes differ')
        return directory / 'content', descriptor

    def verified_root(self, principal, digest):
        principal.require_scope('skills:read')
        try:
            return self._read(principal, digest)[0]
        except OSError as exc:
            raise AutomationError('skills_artifact_integrity', 'Owned frozen package is unavailable') from exc

    def promote(self, principal, staged):
        principal.require_scope('skills:read')
        owner, scope = self._scope(principal, create=True)
        if staged['owner_user_id'] != owner:
            raise PermissionError('Staged skill package ownership differs')
        source = Path(staged['root'])
        seal = validated_skill_package_seal(str(source))
        if seal != staged['seal'] or seal['total_bytes'] > self.maximum_package_bytes:
            raise AutomationError('skills_artifact_integrity', 'Staged package seal or bound differs')
        digest = seal['content_sha256']
        destination = scope / digest
        with locked_path(scope / '.promotion'):
            if destination.exists() or destination.is_symlink():
                existing = self._read(principal, digest)[1]
                # A prior attempt may have renamed complete bytes but lost its
                # directory-sync acknowledgement. Reestablish durability before
                # returning evidence eligible for database acceptance.
                _sync_directory(destination)
                _sync_directory(scope)
                return existing
            if shutil.disk_usage(scope).free - seal['total_bytes'] < self.reserved_free_bytes:
                raise AutomationError('skills_artifact_capacity', 'Artifact volume headroom is insufficient')
            with tempfile.TemporaryDirectory(prefix='.staging-', dir=scope) as temporary:
                stage = Path(temporary)
                _directory(stage)
                content = stage / 'content'
                content.mkdir(mode=0o700)
                directories = {content}
                for relative in seal['files']:
                    target = content / relative
                    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    for parent in target.parents:
                        if parent == stage:
                            break
                        parent.chmod(0o700)
                        directories.add(parent)
                    with (source / relative).open('rb') as incoming:
                        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(descriptor, 'wb') as outgoing:
                            shutil.copyfileobj(incoming, outgoing, length=65536)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                copied = validated_skill_package_seal(str(content))
                if copied != seal:
                    raise AutomationError('skills_artifact_integrity', 'Staged content changed during promotion')
                descriptor = {'contract': 'tofu.skill-artifact/v1', 'ownerId': owner,
                              'tenantId': principal.tenant_id, 'seal': seal}
                with (stage / 'seal.json').open('x', encoding='utf-8') as stream:
                    os.chmod(stream.name, 0o600)
                    json.dump(descriptor, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
                    _sync_directory(directory)
                _sync_directory(stage)
                if shutil.disk_usage(scope).free < self.reserved_free_bytes:
                    raise AutomationError('skills_artifact_capacity', 'Artifact volume headroom changed during staging')
                os.rename(stage, destination)
                _sync_directory(scope)
        return descriptor
