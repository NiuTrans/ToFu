"""Scheduler task-config must forward conversation translation settings.

Root cause (2026-08-29, conversation mtd9ci53zq3xfm): ``build_task_config``
built the scheduled turn's task config from tools_config + conversation
settings but dropped ``autoTranslate`` / ``uiLang``. Every translation
trigger — the incremental gate, the terminal coordinator, the input-path
translator — resolves ``resolve_auto_translate(task['config'])`` /
``resolve_translate_target(task['config'])`` from exactly that dict, so
timer/proactive/project-dispatched turns never translated even when the
conversation had auto-translate on.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from lib.scheduler._shared import build_task_config


def test_forwards_conversation_translation_settings():
    config = build_task_config({}, {'autoTranslate': True, 'uiLang': 'en'})
    assert config['autoTranslate'] is True
    assert config['uiLang'] == 'en'


def test_tools_config_takes_precedence():
    config = build_task_config(
        {'autoTranslate': False, 'uiLang': 'ja'},
        {'autoTranslate': True, 'uiLang': 'en'},
    )
    assert config['autoTranslate'] is False
    assert config['uiLang'] == 'ja'


def test_defaults_match_canonical_off():
    config = build_task_config({}, {})
    assert config['autoTranslate'] is False
    assert config['uiLang'] == ''


def test_resolvers_read_the_forwarded_config():
    """Pin the actual consumer seam: the terminal coordinator and the
    incremental gate resolve from ``task['config']`` alone."""
    from lib.conv_config import resolve_auto_translate, resolve_translate_target

    config = build_task_config({}, {'autoTranslate': True, 'uiLang': 'en'})
    assert resolve_auto_translate(config) is True
    assert resolve_translate_target(config) == 'English'
    off = build_task_config({}, {})
    assert resolve_auto_translate(off) is False
    assert resolve_translate_target(off) == 'Chinese'
