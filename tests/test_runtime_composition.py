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
GENERATED_PROJECT_BRAIN = (
    ROOT / "frontend/src/runtime/project-brain-runtime.generated.js"
)
GENERATED_PAPER_READER = (
    ROOT / "frontend/src/runtime/paper-reader-presenters.generated.js"
)
GENERATED_SETTINGS = (
    ROOT / "frontend/src/runtime/settings-presenters.generated.js"
)
GENERATED_ORCHESTRATION = (
    ROOT / "frontend/src/runtime/orchestration-presenters.generated.js"
)
pytestmark = pytest.mark.unit


def test_runtime_manifest_is_complete_safe_and_ordered() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["version"] == 2
    assert manifest["output"] == "frontend/src/runtime/app-runtime.js"
    assert manifest["sections"]
    assert manifest["lazyBundles"]

    names: list[str] = []
    paths: list[str] = []
    outputs = {manifest["output"]}
    rows = list(manifest["sections"])
    for bundle in manifest["lazyBundles"]:
        assert re.fullmatch(r"[a-z][a-z0-9-]*", bundle["name"])
        assert bundle["output"].startswith("frontend/src/runtime/")
        assert bundle["output"].endswith(".generated.js")
        assert bundle["output"] not in outputs
        outputs.add(bundle["output"])
        assert bundle["moduleImports"]
        assert isinstance(bundle["registryImports"], list)
        assert bundle["runtimeServices"]
        assert isinstance(bundle["runtimeExports"], list)
        assert isinstance(bundle["runtimeBindings"], list)
        rows.extend(bundle["sections"])
    for row in rows:
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
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    generated_outputs = [
        manifest["output"],
        *(bundle["output"] for bundle in manifest["lazyBundles"]),
    ]
    ignore = (ROOT / ".ignore").read_text(encoding="utf-8").splitlines()
    for relative_output in generated_outputs:
        assert (ROOT / relative_output).is_file()
        assert relative_output in ignore


def test_project_brain_sections_ship_only_in_the_lazy_runtime() -> None:
    main = GENERATED_RUNTIME.read_text(encoding="utf-8")
    lazy = GENERATED_PROJECT_BRAIN.read_text(encoding="utf-8")
    marker = "/* ===== migrated source: project-brain.js ===== */"
    assert marker not in main
    assert marker in lazy
    assert "featureRegistry as runtimeScope" in lazy
    assert "import { _i18nLang, t } from '../i18n/index';" in lazy
    assert "runtime dependency is unavailable" in lazy

    feature = (ROOT / "frontend/src/features/project-brain.ts").read_text(
        encoding="utf-8",
    )
    assert "import '../runtime/project-brain-runtime.generated.js';" in feature


def test_paper_media_presenters_ship_only_in_the_paper_lazy_runtime() -> None:
    main = GENERATED_RUNTIME.read_text(encoding="utf-8")
    lazy = (
        ROOT / "frontend/src/runtime/paper-media-presenters.generated.js"
    ).read_text(encoding="utf-8")
    for source in ("paper/podcast.js", "paper/video.js"):
        marker = f"/* ===== migrated source: {source} ===== */"
        assert marker not in main
        assert marker in lazy
    assert "featureRegistry as runtimeScope" in lazy
    assert "runtime dependency is unavailable" in lazy

    feature = (ROOT / "frontend/src/features/paper.ts").read_text(
        encoding="utf-8",
    )
    assert "import '../runtime/paper-media-presenters.generated.js';" in feature


def test_paper_reader_presenters_ship_only_in_the_paper_lazy_runtime() -> None:
    main = GENERATED_RUNTIME.read_text(encoding="utf-8")
    lazy = GENERATED_PAPER_READER.read_text(encoding="utf-8")
    for source in ("paper/report.js", "paper-reader.js"):
        marker = f"/* ===== migrated source: {source} ===== */"
        assert marker not in main
        assert marker in lazy
    assert "featureRegistry as runtimeScope" in lazy
    assert "runtime dependency is unavailable" in lazy
    assert 'data-tofu-action-input="_setPaperDescribeDraft(this)"' in lazy
    assert "runtimeScope._setPaperDescribeDraft = _setPaperDescribeDraft;" in lazy
    generated_actions = re.search(
        r"// BEGIN GENERATED LAZY RUNTIME ACTIONS.*?"
        r"// END GENERATED LAZY RUNTIME ACTIONS",
        lazy,
        re.S,
    )
    assert generated_actions is not None
    assert "runtimeScope._setPaperDescribeDraft = _setPaperDescribeDraft;" in (
        generated_actions.group(0)
    )
    assert "_paperDescribeDraft=this.value" not in lazy

    feature = (ROOT / "frontend/src/features/paper.ts").read_text(
        encoding="utf-8",
    )
    assert "import '../runtime/paper-reader-presenters.generated.js';" in feature


def test_settings_presenters_ship_only_in_the_settings_lazy_runtime() -> None:
    main = GENERATED_RUNTIME.read_text(encoding="utf-8")
    lazy = GENERATED_SETTINGS.read_text(encoding="utf-8")
    for source in (
        "settings.js", "settings/core_panel.js", "widgets/chip_input.js",
        "settings/mcp.js",
    ):
        marker = f"/* ===== migrated source: {source} ===== */"
        assert marker not in main
        assert marker in lazy
    generated_actions = re.search(
        r"// BEGIN GENERATED LAZY RUNTIME ACTIONS — settings-presenters.*?"
        r"// END GENERATED LAZY RUNTIME ACTIONS",
        lazy,
        re.S,
    )
    assert generated_actions is not None
    assert "runtimeScope._mcpSaveServer = _mcpSaveServer;" in (
        generated_actions.group(0)
    )
    feature = (ROOT / "frontend/src/features/settings.ts").read_text(
        encoding="utf-8",
    )
    assert "import '../runtime/settings-presenters.generated.js';" in feature
    assert "// BEGIN GENERATED LAZY RUNTIME PORTS — settings-presenters" in lazy
    assert "let _stgModelRouting = null;" in lazy
    assert "_stgProviders" not in lazy
    assert "runtimeScope._renderProvidersTab = _renderProvidersTab;" in lazy


def test_orchestration_owners_ship_only_in_the_orchestration_lazy_runtime() -> None:
    main = GENERATED_RUNTIME.read_text(encoding="utf-8")
    lazy = GENERATED_ORCHESTRATION.read_text(encoding="utf-8")
    for source in (
        "orchestration-catalog.js",
        "orchestration-studio.js",
        "orchestration.js",
    ):
        marker = f"/* ===== migrated source: {source} ===== */"
        assert marker not in main
        assert marker in lazy

    # The lightweight saved-Flow catalogue and generated API client are typed
    # startup owners; none of their retired classic sources may survive in
    # either runtime.
    for source in (
        "api/orchestration-http-contract.generated.js",
        "api/orchestration-response-contracts.js",
        "api/orchestration-client-methods.js",
        "api/orchestration-endpoint-transport.js",
        "api/orchestration-endpoints.js",
        "api/orchestrations.js",
        "orchestration-flow-catalog.js",
    ):
        marker = f"/* ===== migrated source: {source} ===== */"
        assert marker not in main
        assert marker not in lazy
    prelude = (ROOT / "frontend/src/runtime/sections/_prelude.js").read_text()
    epilogue = (ROOT / "frontend/src/runtime/sections/_epilogue.js").read_text()
    assert "from '../features/orchestration/flow-catalog'" in prelude
    assert "from '../features/orchestration/api-client'" in prelude
    assert "from '../core/feature-flags-loader'" in prelude
    assert "installOrchestrationApiClient(Api);" in epilogue
    assert "api: resolveOrchestrationApiClient" in epilogue
    assert "request: () => Api.request('/api/v1/features'" in epilogue
    assert "window.Api.request" not in epilogue

    assert "const {\n  ORCHESTRATION_LAYOUT_BREAKPOINTS," in lazy
    actions = re.search(
        r"// BEGIN GENERATED LAZY RUNTIME ACTIONS — orchestration-presenters.*?"
        r"// END GENERATED LAZY RUNTIME ACTIONS",
        lazy,
        re.S,
    )
    assert actions is not None
    assert "runtimeScope.openOrchestration = openOrchestration;" in actions.group(0)
    assert (
        "runtimeScope.openTaskMode = orchestrationRegistry.openTaskMode;"
        in actions.group(0)
    )

    feature = (ROOT / "frontend/src/features/orchestration.ts").read_text(
        encoding="utf-8",
    )
    for owner in (
        "./orchestration-core-owners",
        "./orchestration-view-owners",
        "./orchestration-studio-view-owners",
    ):
        assert feature.index(f"import '{owner}';") < feature.index(
            "import '../runtime/orchestration-presenters.generated.js';",
        )


def test_lazy_runtime_has_no_accidental_browser_global_dependencies() -> None:
    result = subprocess.run(
        ["node", "scripts/check_lazy_runtime_bindings.mjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = len(manifest["lazyBundles"])
    assert f"Lazy runtime bindings verified ({expected} bundles)." in result.stdout


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
