"""Regression tests for the UI-language translate target + Japanese detection.

Three behaviours the fix guarantees (owner-requested acceptance criteria):

  1. JAPANESE ON THE TRANSLATE PATH IS NOT SKIPPED AS "already Chinese".
     Kanji-heavy Japanese shares the CJK-ideograph block with Chinese, so the
     script + ratio heuristic wrongly calls it ``zh`` and the old
     is_predominantly_chinese skip gate never translated it. The safety net now
     detects with ``force_fasttext=True`` (the statistical model separates
     ja/zh) and only skips when the reply is genuinely in the target language.

  2. WHEN THE UI LANGUAGE IS ENGLISH, the OUTPUT target resolves to English and
     an English assistant reply is short-circuited (no redundant en→en pass).

  3. DEFAULT FALLBACK to Chinese is unchanged — an old-frontend / headless conv
     that carries no ``uiLang`` still targets Chinese, byte-identical to the
     historical hard-pin.

fastText tests are skipped when ``fast_langdetect`` is not importable so the
suite stays green on a vanilla box (the guarded-optional dep). The pure-Python
resolver tests always run.
"""

import copy
import json

import pytest

import lib.tasks_pkg.auto_translate as at
from lib.conv_config import resolve_translate_target, target_lang_code

pytestmark = pytest.mark.unit


def _ft_available() -> bool:
    try:
        import fast_langdetect  # noqa: F401
        return True
    except Exception:
        return False


_HAVE_FT = _ft_available()

# Kanji-heavy Japanese: >30% CJK ideographs → the old ratio gate called this
# 'zh' and skipped it. fastText resolves it 'ja'.
_JA_KANJI = '本日は晴天なり。日本語の文章を翻訳する必要があります。全部確認してください。'


class _FakeDB:
    """execute(...).fetchone() over a (messages, settings) row; UPDATEs no-op."""

    def __init__(self, messages, settings):
        self._messages = messages
        self._settings = settings

    def execute(self, sql, params=()):
        self._last_sql = sql
        return self

    def fetchone(self):
        s = self._last_sql
        if 'SELECT messages, settings' in s:
            return (json.dumps(self._messages), json.dumps(self._settings))
        if 'SELECT updated_at' in s:
            return (123456,)
        return None


def _spy(monkeypatch):
    """Record whether a whole-message translate worker was spawned and with
    which target. Runs the closure synchronously (no real thread / LLM)."""
    captured = {}

    def _fake_do_translate(task_id, content, target, source, conv_id, msg_idx,
                           field, *, msg_id=None):
        captured['spawned'] = True
        captured['target'] = target
        captured['source'] = source

    monkeypatch.setattr('lib.translate._do_translate', _fake_do_translate)
    # No incremental accumulator in these tests → whole-message path.
    monkeypatch.setattr('lib.translate.finalize_incremental', lambda *a, **k: False)
    monkeypatch.setattr('lib.translate.cancel_incremental', lambda *a, **k: False)
    import lib.database.conversation_repository as repo

    def _load(db, conv_id, **_kwargs):
        return repo.ConversationSnapshot(
            metadata={'settings': json.dumps(db._settings), 'rev': 0},
            messages=copy.deepcopy(db._messages),
            source='test_repository')

    monkeypatch.setattr(repo, 'load_conversation', _load)

    class _SyncThread:
        def __init__(self, target=None, daemon=None, name=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(at.threading, 'Thread', _SyncThread)
    return captured


# ── (3) Pure resolver: default + explicit UI languages ──────────────────────

class TargetResolverTest:
    pass


def test_target_defaults_to_chinese_when_unresolved():
    # No uiLang anywhere → historical Chinese hard-pin preserved.
    assert resolve_translate_target({}) == 'Chinese'
    assert resolve_translate_target(None) == 'Chinese'
    assert resolve_translate_target({'foo': 1}, {'bar': 2}) == 'Chinese'


def test_target_follows_ui_lang():
    assert resolve_translate_target({'uiLang': 'en'}) == 'English'
    assert resolve_translate_target({'uiLang': 'zh'}) == 'Chinese'
    assert resolve_translate_target({'uiLang': 'ja'}) == 'Japanese'
    # First source that defines uiLang wins (config over settings).
    assert resolve_translate_target({'uiLang': 'en'}, {'uiLang': 'zh'}) == 'English'
    # Unknown code → safe fallback to Chinese, never crashes.
    assert resolve_translate_target({'uiLang': 'xx'}) == 'Chinese'


def test_target_lang_code_maps_names_to_detector_codes():
    assert target_lang_code('Chinese') == 'zh'
    assert target_lang_code('English') == 'en'
    assert target_lang_code('Japanese') == 'ja'
    # Unknown name → 'zh' fallback (gate can never stop skipping zh content).
    assert target_lang_code('Klingon') == 'zh'


# ── (1) Japanese is NOT skipped on the translate path ───────────────────────

@pytest.mark.skipif(not _HAVE_FT, reason='fast_langdetect not installed')
def test_japanese_reply_is_translated_not_skipped(monkeypatch):
    """Default Chinese UI + a kanji-heavy Japanese assistant reply. The old
    gate skipped it as 'already Chinese'; now it must be translated (spawned)
    with target=Chinese, source=English."""
    messages = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': _JA_KANJI, '_msgId': 'mJA'},
    ]
    db = _FakeDB(messages, {'autoTranslate': True})   # no uiLang → Chinese
    task = {'id': 't-ja', 'convId': 'c-ja', 'config': {'autoTranslate': True},
            '_assistantMsgId': 'mJA'}
    captured = _spy(monkeypatch)
    at._maybe_auto_translate_assistant('c-ja', _JA_KANJI, 1, db=db, task=task)
    assert captured.get('spawned'), \
        'kanji-heavy Japanese must be translated, not skipped as already-Chinese'
    assert captured['target'] == 'Chinese'
    assert captured['source'] == 'English'


@pytest.mark.skipif(not _HAVE_FT, reason='fast_langdetect not installed')
def test_neuter_old_gate_would_have_skipped_japanese():
    """NEUTER twin: prove the OLD is_predominantly_chinese gate (the code we
    replaced) WOULD have wrongly skipped this Japanese — so the new fastText
    gate is what fixes it, not something incidental."""
    from lib.text_lang import is_predominantly_chinese, detect_language
    # The old gate: Japanese kanji-heavy text reads as 'predominantly Chinese'.
    assert is_predominantly_chinese(_JA_KANJI) is True
    # The new gate: force_fasttext separates ja from zh.
    assert detect_language(_JA_KANJI, force_fasttext=True).code == 'ja'


# ── (2) English UI → target English, English reply short-circuited ──────────

def test_english_ui_reply_short_circuited(monkeypatch):
    """UI language English → target resolves to English; an English assistant
    reply is already in the target, so NO translate worker is spawned (no
    redundant en→en pass)."""
    reply = 'This is a normal English assistant reply with enough length to detect.'
    messages = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': reply, '_msgId': 'mEN'},
    ]
    # uiLang=en lives in settings (what the safety net reads).
    db = _FakeDB(messages, {'autoTranslate': True, 'uiLang': 'en'})
    task = {'id': 't-en', 'convId': 'c-en',
            'config': {'autoTranslate': True, 'uiLang': 'en'},
            '_assistantMsgId': 'mEN'}
    captured = _spy(monkeypatch)
    at._maybe_auto_translate_assistant('c-en', reply, 1, db=db, task=task)
    assert not captured.get('spawned'), \
        'English reply into an English UI is already in target — must skip'


def test_english_ui_translates_non_english_reply(monkeypatch):
    """UI language English + a Chinese assistant reply → target English, and the
    reply is NOT in target, so it IS translated (spawned) with target=English.
    Guards that the English-UI skip above is target-aware, not a blanket skip."""
    zh_reply = '这是一段中文的助手回复，需要翻译成英文给用户查看。'
    messages = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': zh_reply, '_msgId': 'mZH'},
    ]
    db = _FakeDB(messages, {'autoTranslate': True, 'uiLang': 'en'})
    task = {'id': 't-en2', 'convId': 'c-en2',
            'config': {'autoTranslate': True, 'uiLang': 'en'},
            '_assistantMsgId': 'mZH'}
    captured = _spy(monkeypatch)
    at._maybe_auto_translate_assistant('c-en2', zh_reply, 1, db=db, task=task)
    assert captured.get('spawned'), 'Chinese reply into English UI must translate'
    assert captured['target'] == 'English'


# ── (3) Default (no uiLang) still targets Chinese, English reply translated ──

def test_default_ui_targets_chinese(monkeypatch):
    """No uiLang (old frontend / headless) + an English reply → target Chinese
    (unchanged), reply not in target → translated with target=Chinese."""
    reply = 'A plain English assistant reply that should be translated to Chinese.'
    messages = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': reply, '_msgId': 'mD'},
    ]
    db = _FakeDB(messages, {'autoTranslate': True})   # no uiLang
    task = {'id': 't-d', 'convId': 'c-d', 'config': {'autoTranslate': True},
            '_assistantMsgId': 'mD'}
    captured = _spy(monkeypatch)
    at._maybe_auto_translate_assistant('c-d', reply, 1, db=db, task=task)
    assert captured.get('spawned')
    assert captured['target'] == 'Chinese', \
        'absent uiLang must fall back to the historical Chinese hard-pin'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
