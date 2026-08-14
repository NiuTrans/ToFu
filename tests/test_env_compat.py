#!/usr/bin/env python3
"""Tests for lib/env_compat.py — TOFU_* env vars with CHATUI_* legacy aliases.

The rebrand from ChatUI → Tofu moved every env var to the ``TOFU_*`` namespace.
We promise the old ``CHATUI_*`` names keep working as aliases. ``getenv_compat``
delivers that promise centrally: a call site passes only the modern ``TOFU_*``
name and the matching ``CHATUI_*`` alias is honoured automatically. These tests
pin that contract so the alias support can't silently regress.

Run:  pytest tests/test_env_compat.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.env_compat import getenv_compat, getenv_project_compat

pytestmark = pytest.mark.unit


class TestLegacyAlias:
    def test_modern_name_resolves(self, monkeypatch):
        monkeypatch.setenv('TOFU_DB_PATH', '/data/tofu.db')
        assert getenv_compat('TOFU_DB_PATH') == '/data/tofu.db'

    def test_legacy_alias_auto_derived(self, monkeypatch):
        # Call site passes ONLY the modern name; the CHATUI_* alias still wins.
        monkeypatch.delenv('TOFU_DB_PATH', raising=False)
        monkeypatch.setenv('CHATUI_DB_PATH', '/legacy/chatui.db')
        assert getenv_compat('TOFU_DB_PATH') == '/legacy/chatui.db'

    def test_modern_takes_precedence_over_legacy(self, monkeypatch):
        monkeypatch.setenv('TOFU_DB_PATH', '/data/tofu.db')
        monkeypatch.setenv('CHATUI_DB_PATH', '/legacy/chatui.db')
        assert getenv_compat('TOFU_DB_PATH') == '/data/tofu.db'

    def test_empty_modern_falls_through_to_legacy(self, monkeypatch):
        # An empty (not unset) TOFU_* var must not mask the legacy alias.
        monkeypatch.setenv('TOFU_DB_PATH', '')
        monkeypatch.setenv('CHATUI_DB_PATH', '/legacy/chatui.db')
        assert getenv_compat('TOFU_DB_PATH') == '/legacy/chatui.db'


class TestDefaults:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv('TOFU_PG_HOST', raising=False)
        monkeypatch.delenv('CHATUI_PG_HOST', raising=False)
        assert getenv_compat('TOFU_PG_HOST', default='127.0.0.1') == '127.0.0.1'

    def test_default_is_empty_string(self, monkeypatch):
        monkeypatch.delenv('TOFU_NOTHING', raising=False)
        monkeypatch.delenv('CHATUI_NOTHING', raising=False)
        assert getenv_compat('TOFU_NOTHING') == ''


class TestVariadicAndNonTofu:
    def test_first_nonempty_among_names_wins(self, monkeypatch):
        monkeypatch.delenv('TOFU_A', raising=False)
        monkeypatch.delenv('CHATUI_A', raising=False)
        monkeypatch.setenv('TOFU_B', 'beta')
        assert getenv_compat('TOFU_A', 'TOFU_B') == 'beta'

    def test_earlier_name_alias_beats_later_name(self, monkeypatch):
        # TOFU_A unset, but its CHATUI_A alias is set → it wins over TOFU_B,
        # because the alias is checked right after TOFU_A and before TOFU_B.
        monkeypatch.delenv('TOFU_A', raising=False)
        monkeypatch.setenv('CHATUI_A', 'legacy-a')
        monkeypatch.setenv('TOFU_B', 'beta')
        assert getenv_compat('TOFU_A', 'TOFU_B') == 'legacy-a'

    def test_non_tofu_name_looked_up_verbatim(self, monkeypatch):
        # A plain (non-TOFU_) name gets no alias expansion.
        monkeypatch.setenv('PLAIN_VAR', 'x')
        assert getenv_compat('PLAIN_VAR') == 'x'

    def test_explicitly_passed_legacy_name_not_duplicated(self, monkeypatch):
        # Passing both names explicitly still works and doesn't double-read.
        monkeypatch.delenv('TOFU_X', raising=False)
        monkeypatch.setenv('CHATUI_X', 'legacy-x')
        assert getenv_compat('TOFU_X', 'CHATUI_X') == 'legacy-x'


class TestProjectDotenvFallback:
    def test_standalone_tool_sees_project_dotenv(self, tmp_path, monkeypatch):
        (tmp_path / '.env').write_text(
            'TOFU_MESSAGES_ROWS_AUTHORITY=1\n', encoding='utf-8')
        monkeypatch.delenv('TOFU_MESSAGES_ROWS_AUTHORITY', raising=False)
        monkeypatch.delenv('CHATUI_MESSAGES_ROWS_AUTHORITY', raising=False)
        assert getenv_project_compat(
            'TOFU_MESSAGES_ROWS_AUTHORITY', project_root=tmp_path) == '1'

    def test_explicit_process_value_shadows_dotenv(self, tmp_path, monkeypatch):
        (tmp_path / '.env').write_text(
            'TOFU_MESSAGES_ROWS_AUTHORITY=1\n', encoding='utf-8')
        monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '0')
        assert getenv_project_compat(
            'TOFU_MESSAGES_ROWS_AUTHORITY', project_root=tmp_path) == '0'

    def test_quoted_legacy_alias_is_supported(self, tmp_path, monkeypatch):
        (tmp_path / '.env').write_text(
            'CHATUI_MESSAGES_ROWS_AUTHORITY="yes"\n', encoding='utf-8')
        monkeypatch.delenv('TOFU_MESSAGES_ROWS_AUTHORITY', raising=False)
        monkeypatch.delenv('CHATUI_MESSAGES_ROWS_AUTHORITY', raising=False)
        assert getenv_project_compat(
            'TOFU_MESSAGES_ROWS_AUTHORITY', project_root=tmp_path) == 'yes'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
