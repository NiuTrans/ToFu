"""Native response producers over the declared durable artifact lifecycle.

Images are base64 encoded inside immutable artifact blobs, not filesystem links.
Entry points require explicit owners; the only file authority is task root policy.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from lib.artifacts.core import create_artifact, get_artifact, ArtifactNotFoundError
from lib.conversations.repository import get_conversation

ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
from lib.artifacts.image_validation_worker import MIMES


def owned_conversation(conv_id, user_id):
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise PermissionError('Explicit owner required')
    if not conv_id or not get_conversation(conv_id, user_id=user_id, include_messages=False):
        raise PermissionError('Conversation unavailable')


def image_info(data):
    """Decode complete immutable bytes in a caller-owned worker, without quotas."""
    if not data:
        raise ValueError('Image missing')
    from lib.artifacts.image_validation_worker import WORKER_FLAG
    mode = [WORKER_FLAG] if getattr(sys, 'frozen', False) else ['-m', 'lib.artifacts.image_validation_worker']
    process = subprocess.Popen([sys.executable, *mode, str(os.getpid())],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, errors = process.communicate(data)
        try:
            result = json.loads(output)
        except (ValueError, TypeError) as error:
            raise ValueError('Image decoding did not produce a complete result') from error
        if process.returncode != 0 or not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError('Invalid or undecodable PNG/JPEG/WebP/GIF')
        info = result.get('info')
        if (not isinstance(info, dict) or set(info) != {'mime', 'width', 'height', 'sizeBytes', 'sha256'}
                or info.get('mime') not in MIMES.values()
                or any(type(info.get(key)) is not int or info[key] <= 0 for key in ('width', 'height', 'sizeBytes'))
                or info['sizeBytes'] != len(data)
                or info.get('sha256') != hashlib.sha256(data).hexdigest()):
            raise ValueError('Image validation result does not match its source')
        return info
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def read_project_image(path, roots):
    """Reuse complete no-follow snapshot reads; format decoding is independent."""
    from lib.artifacts.response_files import read_project_file
    return read_project_file(path, roots)


def publish_asset(path, alt, *, conv_id, user_id, roots, task_id=''):
    owned_conversation(conv_id, user_id)
    if not isinstance(alt, str) or len(alt) > 1000:
        raise ValueError('alt must be text of at most 1000 characters')
    data = read_project_image(path, roots)
    info = image_info(data)
    # Canonical content deliberately excludes alt/path so repeated publication dedupes.
    content = json.dumps({'version': 1, 'data': base64.b64encode(data).decode('ascii')}, separators=(',', ':'))
    row = create_artifact(conv_id=conv_id, content=content, format='markdown',
                          source='response_asset', title='Response image', task_id=task_id,
                          meta={'responseAsset': {**info, 'version': 1, 'ownerUserId': user_id, 'alt': alt}})
    if row.get('source') != 'response_asset' or row.get('meta', {}).get('responseAsset', {}).get('ownerUserId') != user_id:
        raise ValueError('Image publication collided with incompatible artifact evidence')
    return {**info, 'id': row['id'], 'url': '/api/response-assets/' + row['id'], 'alt': alt}


def load_asset(asset_id, *, user_id, conv_id=None):
    try:
        if not ID.fullmatch(asset_id):
            raise ValueError('Invalid id')
        row = get_artifact(asset_id, response_asset_access=True)
        owned_conversation(row['conv_id'], user_id)
        meta = row.get('meta', {}).get('responseAsset', {})
        if row['source'] != 'response_asset' or meta.get('version') != 1 or meta.get('ownerUserId') != user_id or (conv_id and row['conv_id'] != conv_id):
            raise ValueError('Unavailable')
        payload = json.loads(row['content'])
        if payload.get('version') != 1:
            raise ValueError('Invalid content')
        data = base64.b64decode(payload['data'], validate=True)
        info = image_info(data)
        if any(meta.get(key) != value for key, value in info.items()):
            raise ValueError('Corrupt image')
        return data, info
    except (ArtifactNotFoundError, PermissionError, ValueError, KeyError, TypeError) as exc:
        raise ArtifactNotFoundError('Response asset unavailable') from exc


def validate_rich(payload, *, conv_id, user_id, run_id=None):
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[1] / 'contracts/rich_response_v1.schema.json').read_text())
    # JSON serialization rejects non-finite values even where schema numeric comparisons do not.
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'), sort_keys=True)
    Draft202012Validator(schema).validate(payload)
    if conv_id:
        owned_conversation(conv_id, user_id)
    elif run_id:
        from lib.response_surfaces import resolve_scope
        resolve_scope({'runId': run_id}, user_id=user_id)
        if any(b['type'] in ('image_gallery', 'artifact_reference', 'action_panel', 'choice_prompt', 'form_prompt') for b in payload['blocks']):
            raise PermissionError('Conversation-bound references require a conversation')
    else:
        raise PermissionError('Owned response scope required')
    from lib.response_surfaces.validation import validate_surface_semantics
    validate_surface_semantics(payload)
    ids = set()
    checked_assets = set()
    checked_artifacts = set()
    def asset_reference(target):
        if target not in checked_assets:
            load_asset(target, user_id=user_id, conv_id=conv_id)
            checked_assets.add(target)
    def artifact_reference(target):
        if target in checked_artifacts:
            return
        row = get_artifact(target)
        if row['conv_id'] != conv_id:
            raise PermissionError('Artifact reference unavailable')
        owned_conversation(row['conv_id'], user_id)
        checked_artifacts.add(target)
    for block in payload['blocks']:

        if block['id'] in ids:
            raise ValueError('Duplicate block id')
        ids.add(block['id'])
        kind = block['type']
        if kind == 'table' and any(len(row) != len(block['columns']) for row in block['rows']):
            raise ValueError('Table row width must match columns')

        if kind == 'citations':
            from urllib.parse import urlsplit
            if len({source['id'] for source in block['sources']}) != len(block['sources']):
                raise ValueError('Duplicate citation source id')
            for source in block['sources']:
                url = source['url']
                if not re.match(r'^https?://', url, re.IGNORECASE) or re.search(r'[\s\x00-\x1f\x7f\\\\]', url):
                    raise ValueError('Citation URL must be an unambiguous HTTP(S) URL')
                try:
                    parsed = urlsplit(url)
                    valid = parsed.hostname and parsed.username is None and parsed.password is None
                    _ = parsed.port  # Validate malformed and out-of-range ports.
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError('Citation URL must have a host without credentials')

        if kind == 'data_chart':
            series = block['series']
            points = [point for item in series for point in item['points']]
            if len({item['id'] for item in series}) != len(series):
                raise ValueError('Duplicate chart series id')
            if block['kind'] in ('line', 'scatter') and any('x' not in point for point in points):
                raise ValueError('Cartesian charts require numeric x coordinates')
            if block['kind'] in ('bar', 'pie') and any(not point.get('label') for point in points):
                raise ValueError('Categorical charts require point labels')
            if block['kind'] == 'pie' and (len(series) != 1 or any(point['value'] < 0 for point in points)
                                           or sum(point['value'] for point in points) <= 0):
                raise ValueError('Pie charts require one nonnegative series with a positive total')

        if kind == 'timeline':
            start, end = block['axis']['start'], block['axis']['end']
            lane_ids = {lane['id'] for lane in block['lanes']}
            if not start < end:
                raise ValueError('Timeline axis start must precede end')
            if len(lane_ids) != len(block['lanes']):
                raise ValueError('Duplicate timeline lane id')
            for lane in block['lanes']:
                for interval in lane['intervals']:
                    if not start <= interval['start'] < interval['end'] <= end:
                        raise ValueError('Timeline interval must be ordered within axis bounds')
            for marker in block['markers']:
                if not start <= marker['time'] <= end:
                    raise ValueError('Timeline marker must be within axis bounds')
                if 'laneId' in marker and marker['laneId'] not in lane_ids:
                    raise ValueError('Timeline marker lane unavailable')

        if kind in ('comparison', 'chart'):
            for metric in block['metrics']:
                if metric['status'] == 'passed' and 'budget' in metric and (
                        metric['candidate'] > metric['budget'] if metric['direction'] == 'lower'
                        else metric['candidate'] < metric['budget']):
                    raise ValueError('Declared pass contradicts the absolute budget')
        if kind == 'image_gallery':
            for image in block['images']:
                asset_reference(image['id'])
        if kind == 'artifact_reference':
            artifact_reference(block['artifactId'])
        if kind == 'action_panel':
            for action in block['actions']:
                if action['kind'].endswith('_asset'):
                    asset_reference(action['targetId'])
                else:
                    artifact_reference(action['targetId'])
    return text


def publish_rich(payload, *, conv_id, user_id, task_id=''):
    content = validate_rich(payload, conv_id=conv_id, user_id=user_id)
    row = create_artifact(conv_id=conv_id, content=content, format='rich',
                          source='rich_response', title=payload['title'], task_id=task_id,
                          meta={'richResponse': payload})
    if row.get('source') != 'rich_response' or row.get('format') != 'rich':
        raise ValueError('Rich publication collided with incompatible artifact evidence')
    return row
