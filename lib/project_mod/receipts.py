"""Versioned project write facts shared by device writers and task consumers.

Writers call committed_file only after atomic mutation. The optional envelope
preserves the ordinary tool output. Receivers validate the complete bounded
envelope and bind its paths to the requested write set before projecting it.
No task registry, filesystem authority or conversation state lives here.
"""
from __future__ import annotations

import json
import re
import uuid
from functools import lru_cache
from pathlib import Path, PurePosixPath


class FileReceiptError(ValueError):
    pass


@lru_cache(maxsize=1)
def contract():
    return json.loads((Path(__file__).resolve().parents[2]
                       / 'contracts/project_file_receipts_v1.schema.json').read_text())


def file_receipt_limit(name):
    return int(contract()['x-resource-limits'][name])


def relative_path(value):
    if not isinstance(value, str) or not 0 < len(value) <= 1024:
        raise FileReceiptError('Invalid project file receipt path')
    path = PurePosixPath(value.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or re.match(r'^[a-z]:', str(path), re.I) or str(path) == '.':
        raise FileReceiptError('Project file receipt must be root-relative')
    return str(path)


def committed_file(path, action):
    return {'changeId': uuid.uuid4().hex, 'path': relative_path(path), 'action': action}


def wants_receipts(params):
    return type(params.get('file_receipts_version')) is int and params['file_receipts_version'] == 1


def write_result(params, output, files):
    if wants_receipts(params):
        return {'file_receipts_version': 1, 'output': output, 'file_changes': files}
    return output


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(contract())


def decode_write_receipts(result, requested_paths):
    """Legacy outputs carry no commit proof; malformed new envelopes fail closed."""
    if not isinstance(result, dict) or 'file_receipts_version' not in result:
        return []
    if type(result.get('file_receipts_version')) is not int or next(_validator().iter_errors(result), None):
        raise FileReceiptError('Invalid project write receipt envelope')
    requested = {relative_path(path) for path in requested_paths}
    files = []
    for entry in result['file_changes']:
        path = relative_path(entry['path'])
        if path not in requested:
            raise FileReceiptError('Project write receipt is outside the requested write set')
        files.append({**entry, 'path': path})
    return files


def bound_file_changes(files):
    """Bound reconstructible turn presentation; durable history stays untouched."""
    capacity = file_receipt_limit('files_per_turn')
    return list(files[-capacity:]), len(files) > capacity


def file_change_identity(entry):
    """One identity for live changes, checkpoints and settled turn projection."""
    if not isinstance(entry, dict):
        return ('legacy', str(entry))
    if entry.get('source') == 'overleaf':
        return ('overleaf', entry.get('mcpServer'), entry.get('projectId'), entry.get('path'))
    return ('local', entry.get('rootPath') or entry.get('root') or '', entry.get('path') or '')


def is_host_file_change(entry):
    """External receipts grant no authority to host history or file presence."""
    return (isinstance(entry, dict) and entry.get('source') in (None, 'local')
            and not str(entry.get('rootPath') or '').startswith('remote:'))


def merge_file_changes(*groups):
    """Keep first-seen order and the latest fact for each explicit file identity."""
    merged = {}
    for group in groups:
        for entry in group:
            merged[file_change_identity(entry)] = entry
    return list(merged.values())
