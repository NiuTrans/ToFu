"""Canonical query contract: executable single source for limits and validation."""
import json
from uuid import UUID

from lib.tool_results.tool_result_query_generated import CONTRACT
LIMITS = CONTRACT['limits']
MEDIA_TYPE = CONTRACT['manifestMediaType']
SIGNED_CURSOR_PREFIXES = tuple(
    CONTRACT['cursor'][name] + '.' for name in ('currentPrefix', 'legacyPrefix'))


def retention_limits():
    """Launch-time staging budget may tighten, never raise contract ceilings."""
    from runtime_guards import resolve_resource_budget
    mib = resolve_resource_budget('TOFU_BROWSER_STAGING_MAX_MIB', minimum=1,
                                  maximum=LIMITS['globalBytes'] // (1024 * 1024))
    global_bytes = min(LIMITS['globalBytes'], mib * 1024 * 1024)
    return min(LIMITS['ownerBytes'], global_bytes), global_bytes


def validate_manifest(text):

    # Validation belongs to the application persistence boundary, not imports
    # of the clone-free runtime's pure capture/composition values.
    from lib.storage.errors import StorageError
    try:
        value = json.loads(text)
        if type(value) is not dict or set(value) != set(CONTRACT['manifestRequired']):
            raise ValueError()
        if type(value['resultId']) is not str or str(UUID(value['resultId'])) != value['resultId']:
            raise ValueError()
        if value['executionStatus'] not in CONTRACT['executionStatuses']:
            raise ValueError()
        for key in ('sourceCoverage', 'retentionCoverage'):
            if type(value[key]) is not str or value[key] not in ('complete', 'full', 'partial', 'incomplete', 'unavailable', 'unknown'):
                raise ValueError()
        parts, children = value['parts'], value['children']
        if type(parts) is not list or type(children) is not list or len(parts) + len(children) > LIMITS['scanNodes']:
            raise ValueError()
        from lib.storage_authority_sql.operations_pkg._artifacts import _tool_result_digest
        for part in parts:
            if type(part) is not dict:
                raise ValueError()
            kind = part.get('kind')
            fields = {'text', 'json'}
            expected = {'kind', 'text'} if kind in fields else {'kind', 'artifactRef'} if kind == 'artifact' else {'kind', 'reference', 'mimeType'} if kind == 'media' else set()
            optional = set(CONTRACT['manifestOptionalPartFields'])
            if not expected or not expected <= set(part) or set(part) - expected - optional or any(type(part[k]) is not str for k in expected):
                raise ValueError()
            for key in optional & set(part):
                item = part[key]
                if key in ('sourceOrdinal', 'byteSize', 'omittedCount'):
                    if type(item) is not int or item < 0:
                        raise ValueError()
                elif type(item) is not str or len(item) > 2048:
                    raise ValueError()
                if key == 'sha256' and (len(item) != 64 or any(c not in '0123456789abcdef' for c in item)):
                    raise ValueError()
            if kind == 'json':
                json.loads(part['text'])
            if kind == 'artifact':
                _tool_result_digest(part['artifactRef'])
            if kind == 'media' and ((not part['reference'] and part.get('status') != 'unavailable') or len(part['reference']) > 2048 or len(part['mimeType']) > 128):
                raise ValueError()
        for child in children:
            if type(child) is not dict or set(child) != {'name', 'callId', 'artifactRef'} or any(type(v) is not str for v in child.values()):
                raise ValueError()
            if len(child['name']) > 200 or len(child['callId']) > 200:
                raise ValueError()
            _tool_result_digest(child['artifactRef'])
        if len(text.encode()) > LIMITS['manifestBytes']:
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise StorageError('database_protocol_error', 'Invalid result manifest') from exc
