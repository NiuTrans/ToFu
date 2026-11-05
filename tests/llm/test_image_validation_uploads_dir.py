"""Image validation reads the uploads directory only for local image URLs.

``_validate_image_blocks`` runs on every request body. Resolving
``uploads_root()`` creates that directory, so doing it unconditionally cost a
filesystem round trip per model round even when no message references a local
``/api/images/`` file.
"""
import base64

import pytest

import lib.runtime.runtime_paths as runtime_paths
from lib.llm.body import _validate_image_blocks

pytestmark = pytest.mark.unit

_PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 300


def _image_message(url):
    return [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'look'},
        {'type': 'image_url', 'image_url': {'url': url}},
    ]}]


def test_inline_images_do_not_resolve_the_uploads_directory(monkeypatch):
    resolved = []
    monkeypatch.setattr(runtime_paths, 'uploads_root',
                        lambda: resolved.append(True) or '/unused')
    inline = f'data:image/png;base64,{base64.b64encode(_PNG).decode()}'
    messages = _image_message(inline)

    messages = _validate_image_blocks(messages)

    assert resolved == []
    assert messages[0]['content'][1]['image_url']['url'] == inline


def test_local_image_url_resolves_from_the_uploads_directory(monkeypatch, tmp_path):
    (tmp_path / 'images').mkdir()
    (tmp_path / 'images' / 'shot.png').write_bytes(_PNG)
    resolved = []
    monkeypatch.setattr(runtime_paths, 'uploads_root',
                        lambda: resolved.append(True) or str(tmp_path))
    messages = _image_message('/api/images/shot.png')
    messages.append(_image_message('/api/images/shot.png?v=2')[0])

    messages = _validate_image_blocks(messages)

    assert resolved == [True]
    for message in messages:
        url = message['content'][1]['image_url']['url']
        assert url == f'data:image/png;base64,{base64.b64encode(_PNG).decode()}'
