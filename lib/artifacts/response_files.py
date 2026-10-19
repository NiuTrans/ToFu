"""Explicit existing-file publication over the immutable artifact codec.

Authority: contracts/response_file_v1.schema.json. Verified roots and owner are
host inputs, never inferred from shell output. Images use response_assets.
Entry points: publish_file/publish_response_file, load_file, download_payload.
"""
from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
import io
import json
import os as os  # Existing publication fault-injection seam; shared with file_snapshot.
from pathlib import Path

import struct
import zipfile

from lib.artifacts.core import ArtifactNotFoundError
from lib.response_assets import ID, owned_conversation

WARNING = 'Original HTML/SVG may execute scripts when opened locally. Download only if you trust it.'


@lru_cache(maxsize=1)
def contract():
    return json.loads((Path(__file__).resolve().parents[2] / 'contracts/response_file_v1.schema.json').read_text())


def safe_filename(name):
    name = ''.join(c for c in name if c.isprintable() and c not in '/\\";').strip(' .')
    if len(name) > 200:
        suffix = Path(name).suffix[:20]
        name = name[:200 - len(suffix)] + suffix
    return name or 'response-file'


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError('File publication cancelled')


def read_project_file(path, roots, *, cancelled=None):
    """Read complete bytes through the shared descriptor/freshness boundary."""
    from .file_snapshot import read_project_snapshot
    return read_project_snapshot(path, roots, include_bytes=True, fingerprint=False,
        check=lambda: _check_cancelled(cancelled)).content


def file_info(data, filename):
    extension = Path(filename).suffix.lower()
    spec = contract()['x-policy']['extensions'].get(extension)
    if not spec or not data:
        raise ValueError('Unsupported or empty response file')
    fmt, mime = spec
    if fmt != 'file':
        data.decode('utf-8', errors='strict')
    elif extension == '.pdf':
        if not data.startswith(b'%PDF-'):
            raise ValueError('Invalid PDF signature')
    elif extension in ('.doc', '.xls', '.ppt'):
        if not data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
            raise ValueError('Invalid office signature')
    else:
        if not data.startswith((b'PK\x03\x04', b'PK\x05\x06')):
            raise ValueError('Invalid ZIP signature')
        # Verify the complete central directory before ZipFile allocates members.
        # This codec supports classic single-disk ZIP metadata; format checks are independent of file size.

        end = data.rfind(b'PK\x05\x06', max(0, len(data) - 65557))
        if end < 0 or end + 22 > len(data):
            raise ValueError('Missing ZIP central directory')
        disk, cd_disk, disk_entries, entries, cd_size, cd_offset, comment_size = struct.unpack_from('<4H2LH', data, end + 4)
        if disk or cd_disk or entries != disk_entries or cd_offset + cd_size > end or end + 22 + comment_size != len(data):
            raise ValueError('Invalid ZIP central directory extent')
        cursor, count = cd_offset, 0
        while cursor < cd_offset + cd_size:
            if cursor + 46 > cd_offset + cd_size or data[cursor:cursor + 4] != b'PK\x01\x02':
                raise ValueError('Invalid ZIP central directory member')
            name_len, extra_len, comment_len = struct.unpack_from('<3H', data, cursor + 28)
            cursor += 46 + name_len + extra_len + comment_len
            count += 1
            if cursor > cd_offset + cd_size:
                raise ValueError('ZIP member extends beyond central directory')
        if count != entries:
            raise ValueError('ZIP member count mismatch')
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                member = {'.docx': 'word/document.xml', '.xlsx': 'xl/workbook.xml', '.pptx': 'ppt/presentation.xml',
                          '.odt': 'content.xml', '.ods': 'content.xml', '.odp': 'content.xml'}.get(extension)
                if member and member not in names:
                    raise ValueError('Office package type mismatch')
                if extension in ('.docx', '.xlsx', '.pptx') and '[Content_Types].xml' not in names:
                    raise ValueError('Missing office content types')
                if extension in ('.odt', '.ods', '.odp'):
                    header = archive.getinfo('mimetype')
                    if header.file_size != len(mime.encode('ascii')):
                        raise ValueError('OpenDocument type mismatch')
                    with archive.open(header) as stream:
                        if stream.read(header.file_size + 1).decode('ascii') != mime:
                            raise ValueError('OpenDocument type mismatch')
                if len(archive.infolist()) != entries:
                    raise ValueError('ZIP member count mismatch')
        except (zipfile.BadZipFile, KeyError, UnicodeError, RuntimeError, NotImplementedError) as exc:
            raise ValueError('Invalid ZIP/office package') from exc
    return fmt, {'version': 1, 'kind': 'attachment' if fmt == 'file' else 'document',
                 'filename': safe_filename(filename), 'mime': mime, 'sizeBytes': len(data),
                 'sha256': hashlib.sha256(data).hexdigest(), 'scriptRisk': fmt in ('html', 'svg')}


def public_file_info(info):
    from jsonschema import Draft202012Validator
    fields = contract()['$defs']['publicFile']['properties']
    result = {key: info[key] for key in fields if key in info}
    from jsonschema.exceptions import ValidationError
    try:
        Draft202012Validator(contract()['$defs']['publicFile']).validate(result)
    except ValidationError as exc:
        raise ValueError('Invalid response file metadata') from exc
    if result['filename'] != safe_filename(result['filename']):
        raise ValueError('Unsafe response file filename')
    spec = contract()['x-policy']['extensions'].get(Path(result['filename']).suffix.lower())
    if spec is None or result['mime'] != spec[1] or (result['kind'] == 'attachment') != (spec[0] == 'file') or result['scriptRisk'] != (spec[0] in ('html', 'svg')):
        raise ValueError('Contradictory response file presentation')
    return result


def envelope(data):
    return json.dumps({'version': 1, 'kind': 'response_file', 'encoding': 'base64',
                       'data': base64.b64encode(data).decode('ascii')}, separators=(',', ':'), sort_keys=True)


def publish_file(path, title='', *, conv_id, user_id, roots, task_id='', cancelled=None):
    """Persist bytes, return stable public receipt; caller emits artifact event."""
    _check_cancelled(cancelled)
    from lib.artifacts.core import create_artifact, public_meta
    owned_conversation(conv_id, user_id)
    if not isinstance(title, str) or len(title) > 300:
        raise ValueError('title must be text of at most 300 characters')
    data = read_project_file(path, roots, cancelled=cancelled)
    fmt, info = file_info(data, Path(path).name)
    content = envelope(data) if fmt == 'file' else data.decode('utf-8', errors='strict')
    _check_cancelled(cancelled)
    row = create_artifact(conv_id=conv_id, content=content, format=fmt, source='response_file',
                          title=title or info['filename'], task_id=task_id,
                          meta={'responseFile': {**info, 'ownerUserId': user_id}})
    persisted = row.get('meta', {}).get('responseFile', {})
    if row['source'] != 'response_file' or row['format'] != fmt or persisted.get('ownerUserId') != user_id or any(persisted.get(k) != info[k] for k in ('kind', 'mime', 'sizeBytes', 'sha256', 'scriptRisk')):
        raise ValueError('File publication collided with incompatible artifact evidence')
    info = public_file_info(persisted)  # First filename remains stable on dedupe.
    aid = row['id']
    download = f'/api/artifacts/{aid}/download'
    raw = f'/api/artifacts/{aid}/raw'
    receipt = {**{k: info[k] for k in ('filename', 'mime', 'sizeBytes', 'sha256')},
               'id': aid, 'artifactId': aid, 'format': fmt, 'url': download if fmt == 'file' else raw,
               'downloadUrl': download, 'meta': {'responseFile': info}, 'artifact': public_meta(row)}
    if fmt != 'file':
        receipt['rawUrl'] = raw
    if info['scriptRisk']:
        receipt.update(interactiveDownloadUrl=download + '?interactive=1', warning=WARNING)
    from jsonschema import Draft202012Validator
    Draft202012Validator(contract()).validate(receipt)
    return receipt


publish_response_file = publish_file


def _decode_file_row(row):
    """Pure codec verification, used only after the delivery ownership boundary."""
    try:
        info = row.get('meta', {}).get('responseFile', {})
        if row['source'] != 'response_file':
            raise ValueError('Unavailable')
        public = public_file_info(info)
        if info['kind'] == 'attachment':
            payload = json.loads(row['content'])
            if set(payload) != {'version', 'kind', 'encoding', 'data'} or payload['version'] != 1 or payload['kind'] != 'response_file' or payload['encoding'] != 'base64':
                raise ValueError('Invalid envelope')
            data = base64.b64decode(payload['data'], validate=True)
        else:
            data = row['content'].encode('utf-8', errors='strict')
        fmt, actual = file_info(data, info['filename'])
        if actual != public or fmt != row['format']:
            raise ValueError('Corrupt response file')
        return data, public
    except (ArtifactNotFoundError, PermissionError, ValueError, KeyError, TypeError) as exc:
        raise ArtifactNotFoundError('Response file unavailable') from exc


def load_artifact_for_delivery(artifact_id, *, user_id, conv_id=None):
    """One body query with a live owner check; no caller-supplied row/auth bypass."""

    from lib.identity import require_user_id
    try:
        require_user_id(user_id, context='artifact delivery')
    except ValueError as exc:
        raise ArtifactNotFoundError('Unavailable') from exc
    from lib.artifacts.core import get_artifact
    if not isinstance(artifact_id, str) or not ID.fullmatch(artifact_id):
        raise ArtifactNotFoundError('Unavailable')
    row = get_artifact(artifact_id, file_access=True, user_id=user_id)
    if conv_id is not None and row['conv_id'] != conv_id:
        raise ArtifactNotFoundError('Unavailable')
    info = None
    if row['source'] == 'response_file':
        if row.get('meta', {}).get('responseFile', {}).get('ownerUserId') != user_id:
            raise ArtifactNotFoundError('Unavailable')
        data, info = _decode_file_row(row)
        if row['format'] == 'file':
            row = {**row, 'content': ''}
    else:
        data = row['content'].encode('utf-8')
    return row, data, info


def load_file(artifact_id, *, user_id, conv_id=None):
    row, data, info = load_artifact_for_delivery(artifact_id, user_id=user_id, conv_id=conv_id)
    if row['source'] != 'response_file':
        raise ArtifactNotFoundError('Response file unavailable')
    return data, info


def download_payload(artifact_id, *, user_id, interactive=False, conv_id=None):
    """Resolve an owned original attachment; legacy text uses the same boundary."""
    row, data, info = load_artifact_for_delivery(artifact_id, user_id=user_id, conv_id=conv_id)
    if row['source'] == 'response_file':
        filename, mime, risk = info['filename'], info['mime'], info['scriptRisk']
    else:
        mime = {'html': 'text/html', 'svg': 'image/svg+xml', 'markdown': 'text/markdown', 'rich': 'application/json'}.get(row['format'], 'text/plain')
        extension = {'html': '.html', 'svg': '.svg', 'markdown': '.md', 'rich': '.json'}.get(row['format'], '.txt')
        filename = safe_filename(row.get('title') or 'artifact-' + artifact_id + extension)
        risk = row['format'] in ('html', 'svg')
    if risk and not interactive:
        filename += '.txt'
        mime = 'text/plain'
    return data, {'filename': filename, 'mime': mime, 'warning': WARNING if risk and interactive else ''}
