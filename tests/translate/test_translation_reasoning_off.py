"""Translation requests the smallest reasoning every dispatch path can honor.

Without an explicit intent the reasoning-effort wire emits the model's declared
default rung: kimi-k3.1-pro (cheap-tagged, always-thinking) then reasoned at
``max`` for 92 s before the first translated character.
"""
import pytest

import lib.llm_dispatch as dispatch_facade

pytestmark = pytest.mark.unit
from lib.translate.engine._engine import _dispatch_translation_candidate


@pytest.mark.parametrize('mode', ['stream', 'strict_chat', 'smart_chat'])
def test_every_translation_dispatch_disables_reasoning(monkeypatch, mode):
    seen = {}

    def fake_chat(*_args, **kwargs):
        seen.update(kwargs)
        return 'ok', {}

    def fake_stream(*_args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError('captured')

    monkeypatch.setattr(dispatch_facade, 'dispatch_stream', fake_stream)
    monkeypatch.setattr(dispatch_facade, 'dispatch_chat', fake_chat)
    monkeypatch.setattr(dispatch_facade, 'smart_chat', fake_chat)

    call = dict(
        chunk_label='', max_tokens=64, timeout=5, max_retries=0,
        excluded_models=set(), progress_cb=None,
        deadline_exceeded=lambda: False,
        remaining_deadline_seconds=lambda: 5.0,
        stream=mode == 'stream', strict_model=mode == 'strict_chat',
        prefer_model='m' if mode == 'strict_chat' else None,
    )
    try:
        _dispatch_translation_candidate([{'role': 'user', 'content': 'x'}], **call)
    except RuntimeError:
        pass
    assert seen.get('thinking_enabled') is False
