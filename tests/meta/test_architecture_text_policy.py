"""Architecture admission distinguishes concrete retired mechanisms from glyphs."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_architecture as architecture

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/example.py'}


@pytest.fixture
def source_tree(monkeypatch, tmp_path):
    sources = []
    monkeypatch.setattr(architecture, 'ROOT', tmp_path)
    monkeypatch.setattr(architecture, 'authored_sources', lambda: sources)
    monkeypatch.setattr(architecture, 'RETIRED_PATHS', ())
    monkeypatch.setattr(architecture, 'frontend_legacy_debt_violations', lambda: ([], []))
    monkeypatch.setattr(architecture, 'lib_init_ratchet_violations', lambda: [])
    monkeypatch.setattr(architecture, 'lib_toplevel_ratchet_violations', lambda: [])

    def write(name: str, source: str) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')
        sources.append(path)
        return path

    return write


@pytest.mark.parametrize('name,source', [
    ('label.py', "RATING = '★'\n"),
    ('example.py', 'EXAMPLE = """A Unicode rating:\n★\n"""\n'),
    ('label.js', 'const label = "A user\\\'s ★ rating";\n'),
    ('template.ts', 'const template = `<p>\n// ★ is display text\n</p>`;\n'),
    ('nested.ts', "const label = `Rating: ${visible ? '★' : ''}`;\n"),
])
def test_display_glyph_does_not_claim_architecture_violation(source_tree, capsys, name, source):
    source_tree(name, source)

    assert architecture.main() == 0
    assert 'architecture-check: OK (1 authored files)' in capsys.readouterr().out


@pytest.mark.parametrize('source,reason', [
    ('from lib import push\n', 'use lib.agent_core.push'),
    ('import lib.task_runtime\n', 'use lib.agent_core.task_runtime'),
    ("URL = '/api/v2/conversations'\n", 'conversation v2 HTTP surface is retired'),
    ("URL = '/api/chat/stream'\n", 'use task replay SSE'),
    ("URL = '/api/v1/chat/send'\n", 'use Conversation Sync v3 turn commands'),
    ("REFERENCE = 'pt_abcdef123456'\n", 'incident/board identifiers belong in Git history'),
    ('TurnStoreV2 = object()\n', 'use ConversationTurnStore'),
])
def test_concrete_retired_tokens_still_fail_including_inside_strings(
    source_tree, capsys, source, reason,
):
    source_tree('retired.py', source)

    assert architecture.main() == 1
    assert reason in capsys.readouterr().out


def test_retired_path_is_still_rejected(source_tree, monkeypatch, capsys):
    path = source_tree('retired.py', 'VALUE = 1\n')
    monkeypatch.setattr(architecture, 'RETIRED_PATHS', (path,))

    assert architecture.main() == 1
    assert 'retired.py: retired path exists' in capsys.readouterr().out


def test_reverse_dependency_is_still_rejected(source_tree, capsys):
    source_tree('lib/example.py', 'import routes\n')

    assert architecture.main() == 1
    assert 'backend libraries never import routes' in capsys.readouterr().out
