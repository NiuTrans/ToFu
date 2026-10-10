"""Synthetic image preparation budgets and repeat-build work regression."""
import base64
import copy
import io
import statistics
import time
from unittest.mock import patch

import pytest

Image = pytest.importorskip('PIL.Image')
from lib.llm.body import build_body
from lib.llm.body import _images
from lib.llm.body._image_reuse import ImagePreparationReuse
from lib.tasks_pkg.model_images import pin_model_images, task_image_reuse

pytestmark = pytest.mark.unit
MODEL = 'claude-opus-4-8'


def source(mode='RGB', size=(2200, 1700)):
    buffer = io.BytesIO()
    Image.new(mode, size, (123, 45, 67, 128) if mode == 'RGBA'
              else (123, 45, 67)).save(buffer, format='PNG')
    return [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {
        'url': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()}}]}]


@pytest.mark.parametrize('mode,size', [('RGB', (2200, 1700)),
    ('RGBA', (2200, 1700)), ('RGB', (100, 80)), ('RGBA', (100, 80))])
def test_fresh_build_reuses_original_without_pillow_work(mode, size):
    messages = source(mode, size)
    before = copy.deepcopy(messages)
    reuse = ImagePreparationReuse(user_id=7)
    first = build_body(MODEL, messages, image_reuse=reuse, image_user_id=7)
    with patch.object(Image, 'open', side_effect=AssertionError('decode repeated')) as opened, \
         patch.object(Image.Image, 'resize', side_effect=AssertionError('resize repeated')) as resized, \
         patch.object(Image.Image, 'save', side_effect=AssertionError('encode repeated')) as encoded:
        for _ in range(3):
            assert build_body(MODEL, copy.deepcopy(messages), image_reuse=reuse,
                              image_user_id=7) == first
    # The production best-effort decoder catches Exception, so raising alone
    # cannot prove zero work on an under-cap image whose output stays unchanged.
    assert opened.call_count == resized.call_count == encoded.call_count == 0
    assert messages == before
    assert len(reuse) == 1


def test_budgets_eviction_and_clear():
    reuse = ImagePreparationReuse(user_id=1, max_entries=2, max_bytes=4000)
    keys = [reuse.key(str(i), ('policy',), user_id=1) for i in range(4)]
    for key in keys[:3]:
        reuse.put(key, 'x' * 100)
    assert len(reuse) == 2 and reuse.get(keys[0]) is None
    reuse.put(keys[3], 'y' * 1800)
    assert reuse.retained_bytes <= 4000 and len(reuse) == 1
    reuse.put(keys[0], 'z' * 5000)
    assert reuse.get(keys[0]) is None
    reuse.clear()
    assert len(reuse) == reuse.retained_bytes == 0
    hard = ImagePreparationReuse(user_id=1, max_entries=10**9, max_bytes=10**9)
    assert hard.max_entries == 32 and hard.max_bytes == 16 * 1024**2


def test_policy_model_content_and_owner_evidence(monkeypatch):
    reuse = ImagePreparationReuse(user_id=1)
    messages = source(size=(100, 80))
    def build(model=MODEL, original=messages):
        return build_body(model, original, image_reuse=reuse, image_user_id=1)
    build()
    with patch.object(Image, 'open', wraps=Image.open) as opened:
        build('claude-sonnet-4-6')
        monkeypatch.setattr(_images, '_CLAUDE_IMAGE_MAX_PX', 1500)
        build()
        build(original=source(size=(101, 80)))
        assert opened.call_count == 3
    with pytest.raises(ValueError, match='owner'):
        build_body(MODEL, messages, image_reuse=reuse, image_user_id=2)
    other = task_image_reuse({'_userId': 2}, model=MODEL)
    assert other is not reuse and len(other) == 0


def test_text_then_vision_reads_once_and_retains_original_reference(monkeypatch, tmp_path):
    import lib.knowledge
    import lib.model_info
    import lib.llm.body._build as body_build
    supports = lambda model: model != 'synthetic-text-only'
    monkeypatch.setattr(lib.model_info, 'model_supports_vision', supports)
    monkeypatch.setattr(body_build, 'model_supports_vision', supports)
    raw = base64.b64decode(source()[0]['content'][0]['image_url']['url'].split(',')[1])
    path = tmp_path / 'synthetic.png'
    path.write_bytes(raw)
    calls = []
    def read(ref, *, user_id):
        calls.append((ref, user_id))
        return path
    monkeypatch.setattr(lib.knowledge, 'read_source_path', read)
    messages = [{'role': 'user', 'content': [{'type': 'image_url',
                 'image_url': {'url': 'tofu-media:synthetic'}}]}]
    pin_model_images(messages, model='synthetic-text-only', user_id=7)
    build_body('synthetic-text-only', messages, image_user_id=7)
    assert calls == [] and '_modelImagesPinnedFor' not in messages[0]
    assert messages[0]['content'][0]['image_url']['url'] == 'tofu-media:synthetic'
    for model in (MODEL, MODEL, 'gpt-4o'):
        pin_model_images(messages, model=model, user_id=7)
        build_body(model, messages, image_user_id=7)
    assert calls == [('synthetic', 7)]
    assert messages[0]['_modelImageSources'] == ['tofu-media:synthetic']
    assert base64.b64decode(messages[0]['content'][0]['image_url']['url'].split(',')[1]) == raw
    with pytest.raises(ValueError, match='owner'):
        pin_model_images(messages, model=MODEL, user_id=8)


def test_synthetic_fresh_original_benchmark():
    messages = source()
    reuse = ImagePreparationReuse(user_id=1)
    timings = {}
    for label, cache in [('uncached', None), ('reused', reuse)]:
        build_body(MODEL, messages, image_reuse=cache, image_user_id=1)
        samples = []
        for _ in range(7):
            start = time.perf_counter()
            build_body(MODEL, copy.deepcopy(messages), image_reuse=cache, image_user_id=1)
            samples.append((time.perf_counter() - start) * 1000)
        timings[label] = statistics.median(samples)
    print('synthetic 2200x1700 RGB fresh build median ms:', timings,
          'retained_bytes:', reuse.retained_bytes)
    # Timing is diagnostic, not a machine-dependent wall-clock release gate.
    assert len(reuse) == 1
