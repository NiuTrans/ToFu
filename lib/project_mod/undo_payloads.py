"""Immutable undo content behind the existing session journal authority.

Entries: compact_records and hydrate_record. The caller holds session admission.
JSON payloads are content-addressed and durable before index publication. Reads
verify size/digest and refuse symlinks; malformed/missing evidence fails closed.
No worker, payload cache, independent journal, or automatic history deletion.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
import time

from lib.tools.execution_timing import timing_phase

FIELDS = ('originalContent', 'reversePatch')
KIND = 'undo-payload-v1'


def is_reference(value):
    return isinstance(value, dict) and value.get('kind') == KIND


def validate_reference(value):
    if (not isinstance(value, dict) or set(value) != {'kind', 'digest', 'bytes', 'pack', 'offset'}
            or value['kind'] != KIND or not isinstance(value['digest'], str)
            or not re.fullmatch('[0-9a-f]{64}', value['digest'])
            or type(value['bytes']) is not int or value['bytes'] < 0):
        raise ValueError('Invalid undo payload reference')
    if (not isinstance(value['pack'], str) or not re.fullmatch('[0-9a-f]{64}', value['pack'])
            or type(value['offset']) is not int or value['offset'] < 0):
        raise ValueError('Invalid packed undo payload reference')


def _directory(session_dir):
    return Path(session_dir) / 'undo-payloads-v1'


def _open_directory(session_dir, *, create=False):
    directory = _directory(session_dir)
    if create:
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        parent = os.open(session_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _read_bytes(directory, reference):
    validate_reference(reference)
    descriptor = os.open(reference['pack'] + '.pack', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        offset = reference['offset']
        if not stat.S_ISREG(info.st_mode) or info.st_size < offset + reference['bytes']:
            raise ValueError('Undo payload size/type mismatch')
        handle.seek(offset)
        data = handle.read(reference['bytes'])
        if len(data) != reference['bytes'] or hashlib.sha256(data).hexdigest() != reference['digest']:
            raise ValueError('Undo payload digest mismatch')
    return data


def read_payload(session_dir, reference):
    directory = _open_directory(session_dir)
    try:
        with timing_phase('undo_payload_read', bytes_read=reference['bytes']):
            return json.loads(_read_bytes(directory, reference))
    finally:
        os.close(directory)


def compact_records(session_dir, records, *, buffer_budget=4 * 1024 * 1024):
    """Pack new/legacy contents once; existing references never read old payloads.

    Packing bounds migration filesystem work by payload bytes rather than old
    record count. One chunk is at most the buffer budget or one required payload.
    Every chunk and its directory are durable before the index references it.
    """
    result, pieces, references = [], [], []
    buffered = 0
    directory = None
    seen = {}

    def flush():
        nonlocal buffered, directory
        if not references:
            return
        if directory is None:
            directory = _open_directory(session_dir, create=True)
        digest = hashlib.sha256()
        for piece in pieces:
            digest.update(piece)
        pack = digest.hexdigest()
        temporary = '.payload-' + uuid.uuid4().hex
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with timing_phase('undo_payload_write', bytes_written=buffered, records=len(references)):
                with os.fdopen(descriptor, 'wb') as handle:
                    for piece in pieces:
                        handle.write(piece)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(temporary, pack + '.pack', src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            os.fsync(directory)
            for reference in references:
                reference['pack'] = pack
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        pieces.clear()
        references.clear()
        buffered = 0

    try:
        for record in records:
            compact = dict(record)
            for field in FIELDS:
                if field not in compact:
                    continue
                value = compact[field]
                if is_reference(value):
                    validate_reference(value)
                    continue
                data = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
                digest = hashlib.sha256(data).hexdigest()
                if digest in seen:
                    compact[field] = seen[digest]
                    continue
                if buffered and buffered + len(data) > buffer_budget:
                    flush()
                reference = {'kind': KIND, 'digest': digest, 'bytes': len(data), 'offset': buffered}
                compact[field] = seen[digest] = reference
                pieces.append(data)
                references.append(reference)
                buffered += len(data)
            result.append(compact)
        flush()
        return result
    finally:
        if directory is not None:
            os.close(directory)


def hydrate_record(session_dir, record):
    result = dict(record)
    for field in FIELDS:
        if is_reference(result.get(field)):
            result[field] = read_payload(session_dir, result[field])
            if field == 'originalContent' and not isinstance(result[field], str):
                raise ValueError('Invalid original undo payload')
            if field == 'reversePatch' and (not isinstance(result[field], dict)
                    or not all(isinstance(result[field].get(key), str) for key in ('search', 'replace'))):
                raise ValueError('Invalid reverse undo payload')
    return result


def reclaim_removed(session_dir, before, after):
    """Reclaim only payloads removed by a confirmed index transition, under admission."""
    def references(rows):
        return {value['pack'] for row in rows for key in FIELDS
                if is_reference(value := row.get(key))}
    removed = references(before) - references(after)
    if not removed:
        return
    directory = _open_directory(session_dir)
    try:
        for digest in removed:
            try:
                os.unlink(digest + '.pack', dir_fd=directory)
            except FileNotFoundError:
                pass
        os.fsync(directory)
    finally:
        os.close(directory)


def reclaim_orphans(session_dir, records, *, max_entries, minimum_age_seconds=60):
    """Bounded cold-read cleanup of unreferenced crash artifacts, under admission.

    Referenced packs are durable user state and are never candidates. Young
    unreferenced files remain untouched; only our exact private naming patterns
    qualify. A truncated pass can resume through later cold reads after deletes.
    """
    referenced = {value['pack'] for row in records for field in FIELDS
                  if is_reference(value := row.get(field))}
    try:
        directory = _open_directory(session_dir)
    except FileNotFoundError:
        return 0
    removed = 0
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= max_entries:
                    break
                pack = re.fullmatch(r'([0-9a-f]{64})\.pack', entry.name)
                stage = re.fullmatch(r'\.payload-[0-9a-f]{32}', entry.name)
                if (not pack and not stage) or (pack and pack[1] in referenced):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode)
                            or time.time() - info.st_mtime < minimum_age_seconds):
                        continue
                    os.unlink(entry.name, dir_fd=directory)
                    removed += 1
                except FileNotFoundError:
                    continue
        if removed:
            os.fsync(directory)
    finally:
        os.close(directory)
    return removed
