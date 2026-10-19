"""Response publication contract; temporary artifact authority, never live state."""
import io

from contextlib import nullcontext
import json
import os
from types import SimpleNamespace

import pytest
from PIL import Image
from lib import response_assets as service
from lib.artifacts.core import ArtifactNotFoundError, public_meta

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._artifact_sidecar',)


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(service, 'get_conversation', lambda conv, user_id, **kw: {'id': conv} if (conv, user_id) in [('owned', 7), ('other', 8)] else None)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / 'chart.png'
    Image.new('RGB', (8, 10), 'red').save(path)
    return path


def test_publish_reload_dedupe(artifact_storage, owner, image):
    with nullcontext():
        a = service.publish_asset(str(image), 'chart', conv_id='owned', user_id=7, roots=[str(image.parent)])
        b = service.publish_asset(str(image), 'other alt', conv_id='owned', user_id=7, roots=[str(image.parent)])
        assert a['id'] == b['id']
        data, info = service.load_asset(a['id'], user_id=7)
        assert data == image.read_bytes()
        assert info['mime'] == 'image/png'
        for user_id in [8, 0]:
            with pytest.raises(ArtifactNotFoundError):
                service.load_asset(a['id'], user_id=user_id)
        with pytest.raises(ArtifactNotFoundError):
            service.load_asset(a['id'], user_id=7, conv_id='other')
        with pytest.raises(PermissionError):
            service.publish_asset(str(image), '', conv_id='owned', user_id=8, roots=[str(image.parent)])
        from lib.artifacts.core import get_artifact
        with pytest.raises(ArtifactNotFoundError):
            get_artifact(a['id'])


@pytest.mark.parametrize('format,mime', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp'), ('GIF', 'image/gif')])
def test_actual_mime(format, mime):

    from PIL import features
    if format == 'WEBP' and not features.check('webp'):
        pytest.skip('Host Pillow WebP codec unavailable; publication refuses unsupported bytes')

    if format in ('GIF', 'WEBP'):
        try:
            __import__('PIL.' + ('GifImagePlugin' if format == 'GIF' else 'WebPImagePlugin'))
        except ImportError as exc:
            pytest.skip('Host image codec binary unavailable: ' + str(exc))
    buf = io.BytesIO()
    Image.new('RGB', (4, 4)).save(buf, format=format)
    assert service.image_info(buf.getvalue())['mime'] == mime


@pytest.mark.parametrize('data', [b'', b'not PNG', b'<svg/>', b'x' * (4 * 1024 * 1024 + 1), b'\x89PNG\r\n\x1a\n'], ids=['empty', 'random', 'svg', 'large-invalid', 'truncated-header'])
def test_invalid_image(data):
    with pytest.raises(ValueError):
        service.image_info(data)


def test_complete_large_pixel_image_is_validated_without_changing_parent_policy(monkeypatch):
    output = io.BytesIO()
    Image.new('L', (5000, 5000), 7).save(output, format='PNG')
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 10)
    info = service.image_info(output.getvalue())
    assert (info['width'], info['height']) == (5000, 5000)
    assert Image.MAX_IMAGE_PIXELS == 10


def test_regular_file_boundary(image):
    root = str(image.parent)
    link = image.parent / 'link.png'
    link.symlink_to(image)
    fifo = image.parent / 'pipe.png'
    os.mkfifo(fifo)
    for path in [str(link), str(fifo), str(image.parent / 'missing.png'), '../chart.png']:
        with pytest.raises((OSError, ValueError)):
            service.read_project_image(path, [root])
    with pytest.raises(PermissionError):
        service.read_project_image('/etc/passwd', [root])
    image.write_bytes(b'x' * (4 * 1024 * 1024 + 1))
    assert service.read_project_image(str(image), [root]) == image.read_bytes()


def payload():
    return {'version': 1, 'title': 'Result', 'blocks': [{'type': 'table', 'id': 'table', 'title': 'Evidence', 'columns': ['A'], 'rows': [['1']]}]}


def test_schema_and_reload(artifact_storage, owner):
    with nullcontext():
        row = service.publish_rich(payload(), conv_id='owned', user_id=7)
        from lib.artifacts.core import get_artifact
        loaded = get_artifact(row['id'])
        assert loaded['format'] == 'rich'
        assert json.loads(loaded['content']) == payload()
        assert public_meta(loaded)['meta']['richResponse'] == payload()
        assert service.publish_rich(payload(), conv_id='owned', user_id=7)['id'] == row['id']


@pytest.mark.parametrize('mutation', ['version', 'extra', 'duplicate', 'row', 'nan', 'many-duplicate-ids', 'action'])
def test_reject_rich(mutation, owner):
    p = payload()
    if mutation == 'version': p['version'] = 2
    if mutation == 'extra': p['unexpected'] = True
    if mutation == 'duplicate': p['blocks'].append(dict(p['blocks'][0]))
    if mutation == 'row': p['blocks'][0]['rows'] = [['1', '2']]
    if mutation == 'nan': p['blocks'][0]['rows'] = [[float('nan')]]
    if mutation == 'many-duplicate-ids': p['blocks'] *= 25
    if mutation == 'action': p['blocks'] = [{'type': 'action_panel', 'id': 'a', 'title': 'a', 'actions': [{'kind': 'open_asset', 'label': 'x', 'targetId': '../bad'}]}]
    with pytest.raises(Exception):
        service.validate_rich(p, conv_id='owned', user_id=7)


def test_corrupt_storage(owner, monkeypatch, image):
    info = service.image_info(image.read_bytes())
    row = {'conv_id': 'owned', 'source': 'response_asset', 'meta': {'responseAsset': {**info, 'version': 1, 'ownerUserId': 7}}, 'content': '{"version":1,"data":"bad"}'}
    monkeypatch.setattr(service, 'get_artifact', lambda *a, **kw: row)
    with pytest.raises(ArtifactNotFoundError):
        service.load_asset('valid', user_id=7)


def test_hook_and_catalog(owner, image, monkeypatch):
    from lib.tools import rich_response as tools
    ctx = SimpleNamespace(durable_state_available=True, conv_id='owned', owner_user_id=7)
    assert {t['function']['name'] for t in tools.build_tools(ctx)} == tools.NAMES
    called = []
    monkeypatch.setattr(tools, 'publish_for_task', lambda *args: called.append(args) or {'id': 'stable', 'url': '/api/response-assets/stable'})
    assert tools.image_posthook({}, 'read_files', {'path': str(image)}, str(image.parent), {})[0]['id'] == 'stable'
    assert tools.image_posthook({}, 'write_file', {'path': str(image)}, str(image.parent), 'OK')[0]['id'] == 'stable'
    assert tools.image_posthook({}, 'run_command', {'command': str(image)}, str(image.parent), str(image)) == []
    assert len(called) == 2


def test_route_bytes_headers(owner, image, monkeypatch):
    import asyncio
    from quart import Quart
    import routes.response_assets as route
    from routes.api_v1.auth import require_auth
    monkeypatch.setattr(route, 'request_user_id', lambda: 7)
    monkeypatch.setattr(route, 'load_asset', lambda asset_id, user_id: (image.read_bytes(), service.image_info(image.read_bytes())))
    app = Quart(__name__)
    # Test the raw handler, bypass only authentication (which is exercised by the existing auth suite).
    handler = route.response_asset
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    async def invoke():
        async with app.test_request_context('/api/response-assets/id?download=%E5%9B%BE%0D%0A.png'):
            response = await handler('id')
            assert await response.get_data() == image.read_bytes()
            assert response.content_type == 'image/png'
            assert response.headers['X-Content-Type-Options'] == 'nosniff'
            assert response.headers['Cache-Control'].startswith('private')
            assert '%E5%9B%BE' in response.headers['Content-Disposition']
            assert '\r' not in response.headers['Content-Disposition']
    asyncio.run(invoke())


def timeline_payload():
    from pathlib import Path
    return json.loads((Path(__file__).resolve().parents[2] /
                       'designs/ui-gallery/live-photo-timeline.fixture.json').read_text())


def test_timeline_publication_and_reload(artifact_storage, owner):
    from lib.artifacts.core import get_artifact
    p = timeline_payload()
    row = service.publish_rich(p, conv_id='owned', user_id=7)
    assert json.loads(get_artifact(row['id'])['content']) == p


@pytest.mark.parametrize('mutation', ['axis', 'interval', 'outside', 'marker', 'lane', 'duplicate', 'nan', 'styles'])
def test_timeline_rejects_invalid_coordinates_and_markup(mutation, owner):
    from jsonschema import ValidationError
    p = timeline_payload()
    block = p['blocks'][0]
    if mutation == 'axis': block['axis']['end'] = block['axis']['start']
    if mutation == 'interval': block['lanes'][0]['intervals'][0]['end'] = -2
    if mutation == 'outside': block['lanes'][0]['intervals'][0]['start'] = -3
    if mutation == 'marker': block['markers'][0]['time'] = 3
    if mutation == 'lane': block['markers'][0]['laneId'] = 'missing'
    if mutation == 'duplicate': block['lanes'].append(block['lanes'][0])
    if mutation == 'nan': block['axis']['start'] = float('nan')
    if mutation == 'styles': block['css'] = 'body{display:none}'
    if mutation == 'many-duplicate-ids': block['markers'] *= 33
    with pytest.raises((ValueError, ValidationError)):
        service.validate_rich(p, conv_id='owned', user_id=7)


def components_payload():
    from pathlib import Path
    return json.loads((Path(__file__).resolve().parents[2] /
                       'designs/ui-gallery/rich-components.fixture.json').read_text())


def test_chart_citation_publication_reload(artifact_storage, owner):
    from lib.artifacts.core import get_artifact
    payload = components_payload()
    published = service.publish_rich(payload, conv_id='owned', user_id=7)
    assert json.loads(get_artifact(published['id'])['content']) == payload


@pytest.mark.parametrize('mutation', ['series-id', 'missing-x', 'missing-label', 'negative-pie', 'zero-pie',
                                     'multi-pie', 'url', 'credentials', 'backslash', 'port', 'duplicate-source'])
def test_chart_citation_rejects_invalid_evidence(mutation, owner):
    payload = components_payload()
    chart, bar, pie, sources = payload['blocks'][0], payload['blocks'][2], payload['blocks'][3], payload['blocks'][4]
    if mutation == 'series-id': chart['series'].append(chart['series'][0])
    if mutation == 'missing-x': del chart['series'][0]['points'][0]['x']
    if mutation == 'missing-label': del bar['series'][0]['points'][0]['label']
    if mutation == 'negative-pie': pie['series'][0]['points'][0]['value'] = -1
    if mutation == 'zero-pie':
        for point in pie['series'][0]['points']: point['value'] = 0
    if mutation == 'multi-pie': pie['series'].append({**pie['series'][0], 'id': 'other'})
    if mutation == 'url': sources['sources'][0]['url'] = 'javascript:alert(1)'
    if mutation == 'credentials': sources['sources'][0]['url'] = 'https://user:secret@fixture.example'
    if mutation == 'backslash': sources['sources'][0]['url'] = 'https://fixture.example\\path'
    if mutation == 'port': sources['sources'][0]['url'] = 'https://fixture.example:invalid'
    if mutation == 'duplicate-source': sources['sources'].append(sources['sources'][0])
    with pytest.raises(ValueError):
        service.validate_rich(payload, conv_id='owned', user_id=7)


def test_large_valid_image_publish_and_reload_after_source_removal(artifact_storage, owner, tmp_path):
    import hashlib
    path = tmp_path / 'large-source.png'
    Image.frombytes('RGB', (1500, 1500), os.urandom(1500 * 1500 * 3)).save(path)
    data = path.read_bytes()
    assert len(data) > 4 * 1024 * 1024
    receipt = service.publish_asset(str(path), 'complete large image',
        conv_id='owned', user_id=7, roots=[str(tmp_path)])
    path.unlink()
    restored, info = service.load_asset(receipt['id'], user_id=7)
    assert restored == data
    assert info['sizeBytes'] == len(data)
    assert info['sha256'] == hashlib.sha256(data).hexdigest()


def test_corrupt_image_frame_still_refused(image):
    with pytest.raises(ValueError):service.image_info(image.read_bytes()[:-18])


def test_frozen_image_worker_dispatch_is_before_application_startup(monkeypatch):
    import sys
    from lib.artifacts import image_validation_worker as worker
    from lib.paper.builds.workers import dispatch_frozen_worker
    captured = []
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'argv', ['Tofu.exe', worker.WORKER_FLAG, '123'])
    monkeypatch.setattr(worker, 'main', lambda args: captured.append(args) or 29)
    with pytest.raises(SystemExit) as result:dispatch_frozen_worker()
    assert result.value.code == 29 and captured == [['123']]


@pytest.mark.skipif(__import__('sys').platform != 'linux', reason='Linux parent-death binding')
def test_image_worker_dies_when_its_owner_is_abruptly_killed(tmp_path):
    import subprocess
    import sys
    import time
    import psutil
    info = tmp_path / 'worker.pid'
    code = """
import os,subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-m','lib.artifacts.image_validation_worker',str(os.getpid())],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
Path(sys.argv[1]).write_text(str(p.pid))
while True:time.sleep(.1)
"""
    owner = subprocess.Popen([sys.executable, '-c', code, str(info)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = None
    try:
        deadline = time.monotonic() + 5
        while not info.exists() and time.monotonic() < deadline:
            assert owner.poll() is None
            time.sleep(.01)
        worker = psutil.Process(int(info.read_text()))
        owner.kill();owner.wait(timeout=5)
        gone, alive = psutil.wait_procs([worker], timeout=3)
        assert not alive or all(child.status() == psutil.STATUS_ZOMBIE for child in alive)
    finally:
        if owner.poll() is None:owner.kill()
        owner.wait(timeout=5)
        if worker is not None:
            try:worker.kill()
            except psutil.NoSuchProcess:pass


def test_retired_image_worker_owner_prevents_decoder_entry(monkeypatch):
    from lib.artifacts import image_validation_worker as worker
    output = io.BytesIO()
    def forbidden(data):pytest.fail('retired owner reached image decoding')
    monkeypatch.setattr(worker, 'inspect_image', forbidden)
    monkeypatch.setattr(worker, 'binary_stream', lambda name, mode: output)
    assert worker.main([str(os.getppid() + 1000000)]) == 1
    assert json.loads(output.getvalue())['ok'] is False


@pytest.mark.parametrize('launcher', ['desktop/launcher.py', 'desktop/agent_launcher.py'])
def test_frozen_image_role_uses_inherited_streams_without_application_startup(image, tmp_path, launcher):
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    code = """
import os,runpy,sys
sys.path.insert(0,sys.argv[1]);launcher=sys.argv[2]
sys.frozen=True;sys.argv=[launcher,'--artifact-image-worker',str(os.getppid())]
sys.stdin=None;sys.stdout=None;sys.stderr=None
runpy.run_path(launcher,run_name='__main__')
"""
    result = subprocess.run([sys.executable, '-c', code, str(root), str(root / launcher)],
        input=image.read_bytes(), cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report['ok'] is True and report['info']['width'] == 8
    assert not (tmp_path / 'data').exists()


def test_complete_rich_json_above_retired_byte_quota(artifact_storage, owner):
    p = {'version': 1, 'title': 'Complete table', 'blocks': [{'type': 'table', 'id': 'complete', 'title': 'Complete', 'columns': ['Column'] * 12, 'rows': [[f'{row}:{column}:' + 'x' * 980 for column in range(12)] for row in range(100)]}]}
    encoded = json.dumps(p, ensure_ascii=False)
    assert len(encoded.encode('utf-8')) > 1024 * 1024
    row = service.publish_rich(p, conv_id='owned', user_id=7)
    restored = service.get_artifact(row['id'])
    assert json.loads(restored['content']) == p


def test_complete_rich_collection_counts_publish_and_reload(artifact_storage, owner, image):
    table = {'type': 'table', 'id': 'table', 'title': 'Complete', 'columns': ['Column'] * 13, 'rows': [[f'{row}:{column}' for column in range(13)] for row in range(101)]}
    blocks = [{**table, 'id': f'table{i}'} for i in range(25)]
    blocks += [{'type': 'data_chart', 'id': 'chart', 'title': 'Complete', 'kind': 'line', 'series': [{'id': f's{i}', 'label': f'Series {i}', 'points': [{'x': p, 'value': i+p} for p in range(65)]} for i in range(9)]}, {'type': 'citations', 'id': 'sources', 'title': 'Complete', 'sources': [{'id': f's{i}', 'title': f'Source {i}', 'url': f'https://example.com/{i}'} for i in range(33)]}, {'type': 'timeline', 'id': 'timeline', 'title': 'Complete', 'axis': {'start': 0, 'end': 100, 'unit': 's'}, 'lanes': [{'id': f'l{i}', 'label': 'Lane', 'kind': 'other', 'intervals': [{'start': p, 'end': p+0.5, 'label': str(p)} for p in range(33)]} for i in range(13)], 'markers': [{'time': p, 'label': str(p)} for p in range(33)]}]
    asset = service.publish_asset(str(image), 'Complete', conv_id='owned', user_id=7, roots=[str(image.parent)])
    reports = [service.create_artifact(conv_id='owned', content=f'Complete report {i}', format='markdown', source='write_file') for i in range(9)]
    blocks += [{'type': 'image_gallery', 'id': 'images', 'title': 'Complete', 'images': [{'id': asset['id'], 'alt': str(i)} for i in range(13)]}, {'type': 'evidence', 'id': 'evidence', 'title': 'Complete', 'items': [{'label': str(i), 'value': str(i)} for i in range(33)]}, {'type': 'chart', 'id': 'metrics', 'title': 'Complete', 'metrics': [{'label': str(i), 'baseline': i, 'candidate': i+1, 'unit': 'ms', 'direction': 'lower', 'status': 'unknown'} for i in range(33)]}, {'type': 'action_panel', 'id': 'actions', 'title': 'Complete', 'actions': [{'kind': 'open_artifact', 'label': str(i), 'targetId': report['id']} for i, report in enumerate(reports)]}]
    p = {'version': 1, 'title': 'Complete', 'blocks': blocks}
    row = service.publish_rich(p, conv_id='owned', user_id=7)
    assert json.loads(service.get_artifact(row['id'])['content']) == p
