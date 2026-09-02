"""Executable contract for the retained-runtime authoring boundary.

The browser still needs one lexical module while classic bindings are migrated,
but models edit small, named sections. The generated concatenation must never
become an independent source of truth again.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import subprocess

import pytest

from tests._runtime_sections import runtime_section_names


ROOT = Path(__file__).resolve().parents[1]
SECTIONS = ROOT / "frontend/src/runtime/sections"
MANIFEST = SECTIONS / "manifest.json"
GENERATED_RUNTIME = ROOT / "frontend/src/runtime/app-runtime.js"
pytestmark = pytest.mark.unit


def test_runtime_manifest_is_complete_safe_and_ordered() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["output"] == "frontend/src/runtime/app-runtime.js"
    assert manifest["sections"]

    names: list[str] = []
    paths: list[str] = []
    for row in manifest["sections"]:
        name = row["source"]
        relative_path = row["path"]
        assert name.endswith(".js")
        assert "\\" not in relative_path
        assert not Path(relative_path).is_absolute()
        assert ".." not in Path(relative_path).parts
        source_path = (SECTIONS / relative_path).resolve()
        assert SECTIONS.resolve() in source_path.parents
        source = source_path.read_text(encoding="utf-8")
        assert source.startswith(f"/* ===== migrated source: {name} ===== */\n")
        names.append(name)
        paths.append(relative_path)

    assert len(names) == len(set(names))
    assert len(paths) == len(set(paths))


def test_generated_runtime_is_fresh_and_hidden_from_default_discovery() -> None:
    result = subprocess.run(
        ["node", "scripts/compose_frontend_runtime.mjs", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert GENERATED_RUNTIME.is_file()

    ignore = (ROOT / ".ignore").read_text(encoding="utf-8").splitlines()
    assert "frontend/src/runtime/app-runtime.js" in ignore


def test_literal_runtime_section_references_resolve_to_live_owners() -> None:
    """A deleted section must delete or migrate every test that names it."""
    live_names = set(runtime_section_names())
    missing: list[str] = []
    for test_path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(test_path.read_text(encoding="utf-8"), test_path)
        for call in (node for node in ast.walk(tree)
                     if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Name):
                continue
            if call.func.id not in {"runtime_section", "runtime_section_path"}:
                continue
            if not call.args or not isinstance(call.args[0], ast.Constant):
                continue
            section_name = call.args[0].value
            if isinstance(section_name, str) and section_name not in live_names:
                missing.append(
                    f"{test_path.relative_to(ROOT)}:{call.lineno}: {section_name}",
                )
    assert not missing, (
        "tests reference deleted retained-runtime owners:\n"
        + "\n".join(missing)
    )


def test_tests_do_not_recreate_retired_conversation_owner_vocabulary() -> None:
    """Old harness names make a removed message-document owner look current."""
    retired_paths = (
        "ui/chat_render.js",
        "ui/streaming_ui.js",
        "ui/translation_render.js",
        "conv_view.js",
        "ui/message_actions.js",
        "stream_session.js",
        "conv_window.js",
        "core/cross_tab_sync.js",
        "core/conv_state_reducer.js",
        "core/conversations.js",
    )
    retired_symbols = (
        "loadConversationsFromServer",
        "loadConversationMessages",
        "syncConversationToServer",
        "hydrateSidebarFromCache",
        "mergeServerConvShells",
        "_saveConvToolState",
        "_syncToolStateDebounced",
        "_restoreConvToolState",
        "patchToolState",
    )
    intentional_contracts = {
        Path(__file__).name,
        "test_turn_store_owner_migration.py",
        "test_frontend_conversation_catalog.py",
    }
    stale: list[str] = []
    for test_path in sorted((ROOT / "tests").glob("test_*.py")):
        if test_path.name in intentional_contracts:
            continue
        source = test_path.read_text(encoding="utf-8")
        for fragment in (*retired_paths, *retired_symbols):
            if fragment in source:
                stale.append(f"{test_path.name}: {fragment}")
    assert not stale, (
        "tests describe retired conversation owners instead of current ones:\n"
        + "\n".join(stale)
    )


def test_frontend_tests_do_not_recreate_shell_transcripts() -> None:
    """Test harnesses must teach the same normalized ownership as production."""
    retired_access = re.compile(
        r"\b(?:conv|conversation|CONV|c[A-Z]?)\.messages\b",
    )
    intentional_negative_contracts = {
        "test_frontend_authoritative_composer.py",
        "test_frontend_casee_no_autostart_after_ghost_delete.py",
        "test_frontend_conversation_catalog.py",
        "test_frontend_translate_guard.py",
        "test_frontend_turn_nav_navigation.py",
        "test_frontend_turn_projection_vite.py",
        "test_turn_projection_swarm_carryover.py",
        "test_turn_store_owner_migration.py",
    }
    stale: list[str] = []
    paths = list((ROOT / "tests").glob("test_frontend_*.py"))
    paths.extend((ROOT / "tests" / name) for name in (
        "test_turn_projection_swarm_carryover.py",
        "test_turn_store_owner_migration.py",
    ))
    for test_path in sorted(paths):
        if test_path.name in intentional_negative_contracts:
            continue
        for line_number, line in enumerate(
            test_path.read_text(encoding="utf-8").splitlines(), start=1,
        ):
            if retired_access.search(line):
                stale.append(
                    f"{test_path.name}:{line_number}: {line.strip()}",
                )
    assert not stale, (
        "frontend tests recreated a transcript on the conversation shell:\n"
        + "\n".join(stale)
    )
