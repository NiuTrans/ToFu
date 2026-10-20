"""Owner-scoped streaming DFS queries with signed, immutable route cursors.

A cursor stores only the current root-relative route and segment position. Seek
reads at most depth+1 nodes, never replays completed siblings or body prefixes.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import time
from lib.storage.errors import StorageError
from lib.tool_results.tool_result_query_contract import CONTRACT, LIMITS, MEDIA_TYPE, validate_manifest

MAX_BYTES = LIMITS['scanBytes']
WINDOW = 65536


def invalid(message='Invalid continuation cursor', *, kind='invalid_cursor'):
    failure = CONTRACT['failures'][kind]
    raise StorageError(failure['storageCode'], message,
                       retryable=failure['retryable'])


def cursor_key(session):
    row = session.fetch_one('SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?', ('tool_result_private', 'cursor-key-v1'))
    if not row:
        invalid('Continuation key unavailable', kind='artifact_query_unavailable')
    return bytes.fromhex(json.loads(row['value_json']))


def binding(payload, operation):
    return [int(payload['user_id']), str(payload['artifact_ref']), operation,
            str(payload.get('query', '')) if operation == 'search' else '',
            str(payload.get('selector', ''))]


def _scoped_mac(key, payload, operation, data):
    """Bind the complete query without repeating its text inside the cursor."""
    mac = hmac.new(key, digestmod=hashlib.sha256)
    mac.update(CONTRACT['cursor']['authentication']['domain'].encode('utf-8'))
    mac.update(b'\0')
    mac.update(json.dumps(binding(payload, operation), ensure_ascii=False,
                          separators=(',', ':')).encode('utf-8'))
    mac.update(b'\0')
    mac.update(data)
    return mac.digest()


def _base64url(data):
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def _decode_base64url(text):
    data = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    if _base64url(data) != text:
        invalid()
    return data


def encode_cursor(session, payload, operation, state):
    data = json.dumps(state, separators=(',', ':')).encode('utf-8')
    signature = _scoped_mac(cursor_key(session), payload, operation, data)
    token = '.'.join((CONTRACT['cursor']['currentPrefix'], _base64url(data),
                      _base64url(signature)))
    if len(token) > CONTRACT_CURSOR_MAX:
        invalid('Continuation metadata too large', kind='artifact_query_unavailable')
    return token


CONTRACT_CURSOR_MAX = CONTRACT['cursor']['maxLength']


def decode_cursor(session, payload, operation):
    value = payload.get('cursor', payload.get('offset', 0))
    if type(value) is int and 0 <= value <= (1 << 64) - 1:
        raw = str(value)
    elif isinstance(value, str) and len(value) <= CONTRACT_CURSOR_MAX:
        raw = value or '0'
    else:
        invalid()
    if raw.isdigit():
        if operation == 'search' and int(raw):
            invalid()
        return {'r': [], 'p': 0, 'o': 0, 'skip': int(raw), 'expiry': 0, 'partial': False}
    try:
        prefix, token, signature = raw.split('.')
        if prefix == CONTRACT['cursor']['currentPrefix']:
            data = _decode_base64url(token)
            supplied = _decode_base64url(signature)
            expected = _scoped_mac(cursor_key(session), payload, operation, data)
            if not hmac.compare_digest(supplied, expected):
                invalid()
            state = json.loads(data)
        elif prefix == CONTRACT['cursor']['legacyPrefix']:
            data = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4))
            expected = hmac.new(cursor_key(session), data, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                invalid()
            scope, state = json.loads(data)
            if scope != binding(payload, operation):
                invalid()
        else:
            invalid()
        if type(state) is not dict or set(state) != {'r', 'p', 'o', 'skip', 'expiry', 'partial'}:
            invalid()
        if type(state['r']) is not list or len(state['r']) > LIMITS['depth'] + 1 or any(type(n) is not int or n < 0 or n >= LIMITS['scanNodes'] for n in state['r']):
            invalid()
        if any(type(state[k]) is not int or state[k] < 0 for k in ('p', 'o', 'skip', 'expiry')) or type(state['partial']) is not bool:
            invalid()
        if state['expiry'] and state['expiry'] <= int(payload['now_ms']):
            invalid('Continuation evidence expired; start a new query', kind='continuation_unavailable')
        return state
    except (ValueError, TypeError, KeyError):
        invalid()


def select_json(text, selector):
    if not selector:
        return text
    if not isinstance(selector, str) or not selector.startswith('/') or len(selector) > 1024:
        invalid('Invalid JSON pointer', kind='invalid_selector')
    tokens = selector[1:].split('/')
    if len(tokens) > LIMITS['pointerDepth']:
        invalid('JSON pointer depth exceeded', kind='invalid_selector')
    value = json.loads(text)
    import re
    for token in tokens:
        if re.search(r'~(?![01])', token):
            invalid('Invalid JSON pointer escape', kind='invalid_selector')
        key = token.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list):
            if not key.isdigit() or (len(key) > 1 and key[0] == '0'):
                raise KeyError(key)
            value = value[int(key)]
        else:
            value = value[key]
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def query_result(session, payload, operation):
    from ._common import _integer, _required_text
    from ._artifacts import _tool_result_digest
    owner = _integer(payload, 'user_id', minimum=1)
    now = _integer(payload, 'now_ms', minimum=1)
    root = _required_text(payload, 'artifact_ref', 96)
    _tool_result_digest(root)
    needle = _required_text(payload, 'query', 200).casefold() if operation == 'search' else ''
    state = decode_cursor(session, payload, operation)
    initial = dict(state, r=list(state['r']))
    limit = _integer(payload, 'limit', default=8192 if operation == 'read' else 8, minimum=1, maximum=65536 if operation == 'read' else 20)
    deadline = time.monotonic() + LIMITS['scanSeconds']
    cache, issues, media, metadata = {}, [], [], {}
    scanned = nodes = 0
    content, matches, provenance = [], [], []
    emitted = 0
    finished = False

    def issue(path, reason):
        if len(issues) < LIMITS['scanNodes']:
            issues.append({'path': path, 'reason': reason})
        state['partial'] = True

    def load(ref):
        nonlocal nodes, scanned
        if ref in cache:
            return cache[ref]
        digest = _tool_result_digest(ref)
        row = session.fetch_one('SELECT media_type,size_bytes,expires_at_ms FROM tool_result_artifacts WHERE user_id=? AND content_sha256=? AND expires_at_ms>?', (owner, digest, now))
        nodes += 1
        if row is None:
            cache[ref] = None
            return None
        expiry = int(row['expires_at_ms'])
        state['expiry'] = min(state['expiry'] or expiry, expiry)
        manifest = None
        if row['media_type'] == MEDIA_TYPE:
            body = session.fetch_one('SELECT content FROM tool_result_artifacts WHERE user_id=? AND content_sha256=?', (owner, digest))
            manifest = validate_manifest(body['content'])
            scanned += int(row['size_bytes'])
        result = (row, manifest)
        cache[ref] = result
        return result

    first = load(root)
    if first is None:
        if initial['expiry']:
            invalid('Continuation root unavailable', kind='continuation_unavailable')
        return None
    if first[1]:
        metadata = {k: first[1][k] for k in ('resultId', 'executionStatus', 'sourceCoverage', 'retentionCoverage')}

    # Resolve the current route, retaining just ancestors for next-sibling DFS.
    def resolve():
        ref, path, frames, seen = root, [], [], set()
        route = state['r']
        depth = 0
        while True:
            if ref in seen:
                return ('issue', 'cycle', ref, path, frames)
            if depth > LIMITS['depth']:
                return ('issue', 'depth_budget', ref, path, frames)
            seen.add(ref)
            node = load(ref)
            if node is None:
                return ('issue', 'unavailable', ref, path, frames)
            row, manifest = node
            if manifest and path and isinstance(path[-1], dict):
                path[-1] = {**path[-1], 'resultId': manifest['resultId'],
                            'executionStatus': manifest['executionStatus']}
            if not manifest:
                if len(route) != depth:
                    invalid('Continuation route unavailable', kind='continuation_unavailable')
                return ('raw', row, ref, path, frames)
            for key in ('sourceCoverage', 'retentionCoverage'):
                if manifest[key] not in ('complete', 'full'):
                    issue(path, key)
            entries = manifest['parts'] + manifest['children']
            if len(route) <= depth:
                route.append(0)
            index = route[depth]
            if index >= len(entries):
                return ('end', None, ref, path, frames)
            frames.append(len(entries))
            item = entries[index]
            if index < len(manifest['parts']):
                child_path = path + ['/parts/' + str(index)]
                kind = item['kind']
                if kind != 'artifact':
                    if len(route) != depth + 1:
                        invalid('Continuation route unavailable', kind='continuation_unavailable')
                    return (kind, item, ref, child_path, frames)
                ref = item['artifactRef']
            else:
                child_path = path + [{'name': item['name'], 'callId': item['callId']}]
                ref = item['artifactRef']
            path = child_path
            depth += 1

    def advance(frames):
        nonlocal finished
        state['p'] = 0
        route = state['r']
        while frames:
            size = frames.pop()
            route[:] = route[:len(frames) + 1]
            route[-1] += 1
            if route[-1] < size:
                return
        finished = True
        route.clear()

    while not finished:
        # Budget exhaustion retains the not-yet-visited route, never drops it.
        if nodes >= LIMITS['scanNodes'] or scanned >= MAX_BYTES or time.monotonic() >= deadline:
            break
        kind, value, ref, path, frames = resolve()
        if kind in ('issue', 'end', 'media'):
            if kind == 'issue' and value == 'unavailable' and initial['expiry']:
                invalid('Continuation child unavailable; start a new query', kind='continuation_unavailable')
            if kind == 'issue':
                # Missing children never alter route offsets. Once an unavailable
                # node was observed, refusal prevents a later availability shift.
                issue(path, value)
            if kind == 'media':
                if operation == 'read':
                    media.append({**value, 'artifactRef': ref, 'path': path})
                issue(path, 'non_text_media')
            advance(frames)
            continue
        selector = payload.get('selector')
        pos = state['p']
        eof = False
        try:
            if kind == 'raw':
                digest = _tool_result_digest(ref)
                if selector:
                    body = session.fetch_one('SELECT content FROM tool_result_artifacts WHERE user_id=? AND content_sha256=?', (owner, digest))
                    text = select_json(body['content'], selector)
                    piece = text[pos:pos + WINDOW]
                    eof = pos + len(piece) >= len(text)
                else:
                    # Text substring is portable to SQLite and PostgreSQL;
                    # cursor p uses codepoints, external read o uses UTF-8 bytes.
                    body = session.fetch_one('SELECT substr(content,?,?) AS content FROM tool_result_artifacts WHERE user_id=? AND content_sha256=?', (pos + 1, WINDOW + 1, owner, digest))
                    text = body['content']
                    piece, eof = text[:WINDOW], len(text) <= WINDOW
            else:
                if selector and kind != 'json':
                    advance(frames)
                    continue
                text = select_json(value['text'], selector)
                piece = text[pos:pos + WINDOW]
                eof = pos + len(piece) >= len(text)
        except (ValueError, KeyError, IndexError, TypeError):
            issue(path, 'selector_unavailable')
            advance(frames)
            continue
        if not piece:
            advance(frames)
            continue
        scanned += len(piece.encode())
        if operation == 'read':
            encoded = piece.encode()
            skip = min(state['skip'], len(encoded))
            while skip < len(encoded) and encoded[skip] & 0xC0 == 0x80:
                skip += 1
            state['skip'] = max(0, state['skip'] - skip)
            selected = encoded[skip:skip + max(0, limit - emitted)]
            selected = selected.decode('utf-8', errors='ignore')
            if not selected and skip < len(encoded) and emitted == 0:
                selected = encoded[skip:].decode()[0]
            consumed = skip + len(selected.encode())
            if consumed == 0 and emitted:
                # The remaining byte allowance cannot hold the next codepoint.
                # Return this page now instead of rescanning until a scan limit.
                break
            chars = len(encoded[:consumed].decode())
            if selected:
                content.append(selected)
                provenance.append({'artifactRef': ref, 'path': path, 'segment': list(state['r']), 'start': state['o'] + skip, 'end': state['o'] + consumed})
                emitted += len(selected.encode())
            state['p'] += chars
            state['o'] += consumed
            if chars == len(piece) and eof:
                advance(frames)
            if emitted >= limit:
                break
        else:
            # Search folded codepoints with exact source offsets, bounded to a
            # window. Keep overlap only at the window boundary, not page start.
            window, origins = '', []
            consumed = 0
            last_match_start = -1
            for index, char in enumerate(piece):
                if index and index % 4096 == 0 and time.monotonic() >= deadline:
                    break
                for folded in char.casefold():
                    window = (window + folded)[-len(needle):]
                    origins = (origins + [index])[-len(needle):]
                    if window == needle:
                        found = state['o'] + origins[0]
                        if not matches or matches[-1]['offset'] != found:
                            matches.append({'offset': found, 'text': piece[max(0, origins[0]-160):index+321], 'artifactRef': ref, 'path': path})
                            last_match_start = origins[0]
                            if len(matches) >= limit:
                                consumed = origins[0] + 1
                                break
                if len(matches) >= limit:
                    break
                consumed = index + 1
            if len(matches) < limit and (not eof or consumed < len(piece)):
                consumed = max(1, consumed - len(needle), last_match_start + 1)
            state['p'] += consumed
            state['o'] += consumed
            if consumed == len(piece) and eof:
                advance(frames)
            if len(matches) >= limit:
                break
    # A no-progress cutoff is an explicit refusal, never a self-loop cursor.
    if not finished and all(state[k] == initial[k] for k in ('r', 'p', 'o', 'skip')):
        invalid('Query budget too small to make progress', kind='query_budget_exhausted')
    result = {'artifactRef': root, **metadata, 'scope': 'text_and_media_descriptors' if operation == 'read' else 'text_only',
              'coverage': 'incomplete' if state['partial'] or not finished else 'complete',
              'unavailable': issues, 'nextCursor': None, 'truncated': not finished}
    if operation == 'read':
        result.update(content=''.join(content), offset=initial['o'], provenance=provenance, media=media)
        if finished:
            result['sizeBytes'] = state['o']
    else:
        result.update(query=payload['query'], items=matches)
    if not finished:
        if any(item['reason'] == 'unavailable' for item in issues):
            invalid('Continuation child unavailable; start a new query', kind='continuation_unavailable')
        result['nextCursor'] = encode_cursor(session, payload, operation, state)
    result['resumeCursor'] = encode_cursor(session, payload, operation, initial)
    return result
