"""Resolve retained vanilla-JS owners from model-readable runtime sections.

Node/jsdom guards should ask for a logical migrated source name instead of
reaching into either the deleted ``static/js`` tree or the generated 5 MiB
delivery artifact. The ordered files below ``runtime/sections`` are the source
of truth; the composer proves their concatenation is byte-identical to what
Vite receives.
"""

from __future__ import annotations

import atexit
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'frontend' / 'src' / 'runtime' / 'app-runtime.js'
SECTIONS = ROOT / 'frontend' / 'src' / 'runtime' / 'sections'
MANIFEST = SECTIONS / 'manifest.json'
# Lazy chunks carved out of the retained runtime keep their
# `migrated source:` marker so section contracts stay byte-visible here.
RUNTIME_EXTRA = (
    ROOT / 'frontend' / 'src' / 'runtime' / 'scene' / 'tofu-scene.js',
    ROOT / 'frontend' / 'src' / 'runtime' / 'scene' / 'tofu-pet.js',
)
_RETIRED_ORCHESTRATION_API_NAMES = (
    'api/orchestration-http-contract.generated.js',
    'api/orchestration-response-contracts.js',
    'api/orchestration-client-methods.js',
    'api/orchestration-endpoint-transport.js',
    'api/orchestration-endpoints.js',
    'api/orchestrations.js',
)


def shipped_source_text(rel_path: str) -> str:
    """Read shipped source text for anchor guards; the single audited read point."""
    return (ROOT / rel_path).read_text(encoding='utf-8')


@lru_cache(maxsize=1)
def _manifest_rows() -> tuple[dict[str, str], ...]:
    payload = json.loads(MANIFEST.read_text(encoding='utf-8'))
    rows = payload.get('sections')
    lazy_bundles = payload.get('lazyBundles')
    if (payload.get('version') != 2 or not isinstance(rows, list)
            or not isinstance(lazy_bundles, list)):
        raise AssertionError('invalid retained-runtime section manifest')
    combined = list(rows)
    for bundle in lazy_bundles:
        lazy_rows = bundle.get('sections') if isinstance(bundle, dict) else None
        if not isinstance(lazy_rows, list):
            raise AssertionError('invalid lazy retained-runtime bundle')
        combined.extend(lazy_rows)
    return tuple(combined)


def _section_source(name: str) -> str:
    row = next((item for item in _manifest_rows()
                if item.get('source') == name), None)
    if row is not None:
        path = (SECTIONS / str(row.get('path') or '')).resolve()
        if SECTIONS.resolve() not in path.parents:
            raise AssertionError(f'unsafe migrated runtime section path: {name}')
        return path.read_text(encoding='utf-8')
    marker = f'/* ===== migrated source: {name} ===== */'
    for extra in RUNTIME_EXTRA:
        source = extra.read_text(encoding='utf-8')
        if marker in source:
            return source
    raise AssertionError(f'migrated runtime section not found: {name}')
_DIRECTORY = Path(tempfile.mkdtemp(prefix='tofu-runtime-sections-'))
_LEGACY_ROOT = Path(tempfile.mkdtemp(prefix='tofu-frontend-test-root-'))
_CACHE: dict[str, Path] = {}


@atexit.register
def _cleanup() -> None:
    shutil.rmtree(_DIRECTORY, ignore_errors=True)
    shutil.rmtree(_LEGACY_ROOT, ignore_errors=True)


@lru_cache(maxsize=None)
def runtime_section(name: str, *, scope_prelude: bool = True) -> str:
    body = _section_source(name)
    if name == 'core/debug_panel.js':
        # Production imports the bounded typed collection owner, then the thin
        # retained shell port, before loading Debug/Request Inspector. Raw
        # fixtures compose that same owner graph rather than cloning policy.
        state_owner = Path(native_module_path(
            '.native/debug-runtime-owner.js',
            ROOT / 'frontend/src/core/debug-runtime-owner.ts',
        )).read_text(encoding='utf-8')
        body = state_owner + _section_source('core/debug_state.js') + body
    if name == 'local-control.js':
        # Production keeps the badge in the retained runtime and injects live
        # permission flags into the demand-loaded modal. Raw-section fixtures
        # historically execute the complete Local Control behavior in one
        # classic scope, so compose those same two authored ports here instead
        # of recreating their predicates in each jsdom harness.
        retained_badge = _section_source('local-control-state.js')
        state_port = r'''
var LocalControlShellState = Object.freeze({
  get browserEnabled() {
    return typeof browserEnabled !== 'undefined'
      ? Boolean(browserEnabled) : Boolean(globalThis.browserEnabled);
  },
  get desktopEnabled() {
    return typeof desktopEnabled !== 'undefined'
      ? Boolean(desktopEnabled) : Boolean(globalThis.desktopEnabled);
  },
});
'''
        body = (
            retained_badge
            + state_port
            + body
            + '\nruntimeScope.LocalControlPresentationState = '
              'LocalControlPresentationState;\n'
        )
    if name == 'compaction-viewer.js':
        # Production retains only the bounded history projection and injects
        # it into the demand-loaded drawer. Raw-section fixtures historically
        # execute the complete viewer contract, so compose the real authority
        # and its generated runtime ports around the authored presentation.
        state_owner = Path(native_module_path(
            '.native/compaction-history-state.js',
            ROOT / 'frontend/src/core/compaction-history-state.ts',
        )).read_text(encoding='utf-8')
        body = (
            state_owner
            + _section_source('compaction-viewer-state.js')
            + body
            + '\nruntimeScope.CompactionHistoryState = CompactionHistoryState;\n'
              'runtimeScope.getCompactionHistory = getCompactionHistory;\n'
              'runtimeScope.loadCompactionHistory = loadCompactionHistory;\n'
              'runtimeScope.openCompactionViewer = openCompactionViewer;\n'
              'runtimeScope.closeCompactionViewer = closeCompactionViewer;\n'
        )
    if name == 'compaction-viewer-state.js':
        state_owner = Path(native_module_path(
            '.native/compaction-history-state.js',
            ROOT / 'frontend/src/core/compaction-history-state.ts',
        )).read_text(encoding='utf-8')
        body = state_owner + body + (
            '\nruntimeScope.CompactionHistoryState = CompactionHistoryState;\n'
            'runtimeScope.getCompactionHistory = getCompactionHistory;\n'
            'runtimeScope.loadCompactionHistory = loadCompactionHistory;\n'
        )
    if name == 'ui/streaming_swarm_panel.js':
        # Production injects the DOM-free demand scheduler through the ESM
        # prelude. Raw-section fixtures compose that exact typed owner before
        # the retained Swarm projection adapter.
        scheduler_owner = Path(native_module_path(
            '.native/swarm-reconciliation-scheduler.js',
            ROOT / 'frontend/src/conversation/application/'
            'swarm-reconciliation-scheduler.ts',
        )).read_text(encoding='utf-8')
        body = (
            scheduler_owner + body
            + '\nvar SwarmReconciliationTestPort = '
              '_swReconciliationScheduler;\n'
        )
    if name == 'presence.js':
        # Production imports the DOM-free summary/peer state owner through the
        # retained ESM prelude. Raw Collaboration Bar fixtures compose that
        # same typed owner rather than recreating request ordering or bounds.
        presence_owner = Path(native_module_path(
            '.native/presence-summary-controller.js',
            ROOT / 'frontend/src/features/presence-summary-controller.ts',
        )).read_text(encoding='utf-8')
        body = presence_owner + body
    if name == 'core/client_log_relay.js':
        # Production imports the browser-global-free, demand-scoped flush
        # scheduler through the ESM prelude. Raw relay fixtures compose that
        # exact owner ahead of the retained bounded-buffer/transport adapter.
        flush_scheduler = Path(native_module_path(
            '.native/client-log-flush-scheduler.js',
            ROOT / 'frontend/src/core/client-log-flush-scheduler.ts',
        )).read_text(encoding='utf-8')
        body = flush_scheduler + body
    if name == 'core/conversation_invalidation.js':
        revision_gate = Path(native_module_path(
            '.native/conversation-catalog-revision-gate.js',
            ROOT / 'frontend/src/conversation/application/'
            'conversation-catalog-revision-gate.ts',
        )).read_text(encoding='utf-8')
        body = revision_gate + body
    if name == 'project.js':
        # Production imports the browse coordinator in the generated lazy
        # runtime header and injects live retained state through one frozen
        # service object. Raw-section jsdom fixtures need those same two ports
        # ahead of the authored section; a snapshot of projectState would hide
        # the conversation-switch class this boundary is meant to protect.
        coordinator = Path(native_module_path(
            '.native/project-browse-coordinator.js',
            ROOT / 'frontend/src/core/project-browse-coordinator.ts',
        ))
        assets = Path(native_module_path(
            '.native/project-presentation-assets.js',
            ROOT / 'frontend/src/features/project/presentation-assets.ts',
        ))
        state_port = r'''
var ProjectPresentationShellState = Object.freeze({
  get activeConversationId() {
    return typeof activeConvId !== 'undefined'
      ? activeConvId : (globalThis.activeConvId ?? null);
  },
  set activeConversationId(value) {
    if (typeof activeConvId !== 'undefined') activeConvId = value;
    else globalThis.activeConvId = value;
  },
  get conversations() {
    return typeof conversations !== 'undefined'
      ? conversations : (globalThis.conversations || []);
  },
  get projectState() {
    return typeof projectState !== 'undefined'
      ? projectState : (globalThis.projectState || {
          active: false, path: '', extraRoots: [], readOnly: false,
        });
  },
  set projectState(value) {
    if (typeof projectState !== 'undefined') projectState = value;
    else globalThis.projectState = value;
  },
  get sessionStorage() {
    try {
      return typeof sessionStorage !== 'undefined'
        ? sessionStorage : (globalThis.window?.sessionStorage || null);
    } catch (_error) {
      return null;
    }
  },
});
'''
        body = (
            coordinator.read_text(encoding='utf-8')
            + assets.read_text(encoding='utf-8')
            + state_port
            + body
        )
    if name == 'ui/tool_rounds.js':
        # Production imports pure grouping and presentation owners through the
        # retained ESM prelude. Individual classic-section fixtures load and
        # compose those same TypeScript modules instead of rebuilding policy.
        owner_sources = [
            Path(native_module_path(
                '.native/demand-scoped-presentation-ticker.js',
                ROOT / 'frontend/src/conversation/application/'
                'demand-scoped-presentation-ticker.ts',
            )),
            Path(native_module_path(
                '.native/tool-execution-groups.js',
                ROOT / 'frontend/src/conversation/presentation/tool-execution-groups.ts',
            )),
            Path(native_module_path(
                '.native/tool-execution-disclosure.js',
                ROOT / 'frontend/src/conversation/ui/tool-execution-disclosure.ts',
            )),
            Path(native_module_path(
                '.native/tool-round-presentation.js',
                ROOT / 'frontend/src/conversation/presentation/tool-round-presentation.ts',
            )),
            Path(native_module_path(
                '.native/tool-round-icons.js',
                ROOT / 'frontend/src/conversation/presentation/tool-round-icons.ts',
            )),
            Path(native_module_path(
                '.native/turn-provenance.js',
                ROOT / 'frontend/src/conversation/presentation/turn-provenance.ts',
            )),
            Path(native_module_path(
                '.native/write-gate-refusal.js',
                ROOT / 'frontend/src/conversation/presentation/write-gate-refusal.ts',
            )),
            Path(native_module_path(
                '.native/tool-result-presentation.js',
                ROOT / 'frontend/src/conversation/presentation/tool-result-presentation.ts',
            )),
            Path(native_module_path(
                '.native/tool-search-presentation.js',
                ROOT / 'frontend/src/conversation/presentation/tool-search-presentation.ts',
            )),
            Path(native_module_path(
                '.native/tool-image-presentation.js',
                ROOT / 'frontend/src/conversation/presentation/tool-image-presentation.ts',
            )),
            Path(native_module_path(
                '.native/tool-browser-execution-presentation.js',
                ROOT / 'frontend/src/conversation/presentation/tool-browser-execution-presentation.ts',
            )),
            Path(native_module_path(
                '.native/tool-command-execution-presentation.js',
                ROOT / 'frontend/src/conversation/presentation/tool-command-execution-presentation.ts',
            )),
            Path(native_module_path(
                '.native/tool-approval-presentation.js',
                ROOT / 'frontend/src/conversation/presentation/tool-approval-presentation.ts',
            )),
            Path(native_module_path(
                '.native/tool-injection-presentation.js',
                ROOT / 'frontend/src/conversation/presentation/tool-injection-presentation.ts',
            )),
            Path(native_module_path(
                '.native/tool-human-guidance-presentation.js',
                ROOT / 'frontend/src/conversation/presentation/tool-human-guidance-presentation.ts',
            )),
        ]
        composition = r'''
var _testTurnProvenancePresentation = createTurnProvenancePresentation({
  translate: (...args) => typeof t === 'function' ? t(...args) : args[0],
  iconHtml: (...args) => typeof Icon === 'function' ? Icon(...args) : '',
});
var _tpInlineMd = _testTurnProvenancePresentation.inlineMarkdown;
var _testWriteGateRefusalPresentation = createWriteGateRefusalPresentation({
  translate: (...args) => typeof t === 'function' ? t(...args) : args[0],
  iconHtml: (...args) => typeof Icon === 'function' ? Icon(...args) : '',
});
var _refusalInfo = _testWriteGateRefusalPresentation.resolveRefusal;
var _renderGateRefusalBadgeHtml =
  _testWriteGateRefusalPresentation.renderBadgeHtml;
var _renderGateNotice = _testWriteGateRefusalPresentation.renderNoticeHtml;
var _testToolResultPresentation = createToolResultPresentation({
  translate: (...args) => typeof t === 'function' ? t(...args) : args[0],
  writeGateRefusal: _testWriteGateRefusalPresentation,
});
var renderToolResultCompactionLabelHtml =
  _testToolResultPresentation.renderCompactionLabelHtml;
var renderWriteToolResultHtml =
  _testToolResultPresentation.renderWriteResultHtml;
var renderGenericToolResultHtml =
  _testToolResultPresentation.renderGenericResultHtml;
var _testToolSearchPresentation = createToolSearchPresentation({
  translate: (...args) => typeof t === 'function' ? t(...args) : args[0],
  iconHtml: (...args) => typeof Icon === 'function' ? Icon(...args) : '',
});
var renderToolSearchHtml = _testToolSearchPresentation.renderSearchHtml;
var _testToolImagePresentation = createToolImagePresentation({
  translate: (...args) => typeof t === 'function' ? t(...args) : args[0],
  iconHtml: (...args) => typeof Icon === 'function' ? Icon(...args) : '',
});
var renderToolImageHtml = _testToolImagePresentation.renderImageHtml;
var _testToolBrowserExecutionPresentation =
  createToolBrowserExecutionPresentation({
    translate: (...args) => typeof t === 'function' ? t(...args) : args[0],
  });
var renderToolBrowserExecutionHtml =
  _testToolBrowserExecutionPresentation.renderBrowserExecutionHtml;
var _testToolCommandExecutionPresentation =
  createToolCommandExecutionPresentation({
    translate: (...args) => typeof t === 'function' ? t(...args) : args[0],
  });
var renderRunningToolCommandHtml =
  _testToolCommandExecutionPresentation.renderRunningCommandHtml;
var renderSettledToolCommandHtml =
  _testToolCommandExecutionPresentation.renderSettledCommandHtml;
var _testToolApprovalPresentation = createToolApprovalPresentation({
  translate: (...args) => typeof t === 'function' ? t(...args) : args[0],
});
var renderToolApprovalHtml =
  _testToolApprovalPresentation.renderApprovalHtml;
var _testToolInjectionPresentation = createToolInjectionPresentation({
  translate: (...args) => typeof t === 'function' ? t(...args) : args[0],
  renderMarkdown: (source) => typeof renderMarkdown === 'function'
    ? renderMarkdown(source) : escapeHtml(source),
  iconHtml: (...args) => typeof Icon === 'function' ? Icon(...args) : '',
  resolveConversationTitle: (conversationId) =>
    typeof convTitleById === 'function'
      ? convTitleById(conversationId) : conversationId,
});
var renderToolInjectionHtml =
  _testToolInjectionPresentation.renderInjectionHtml;
var _testToolHumanGuidancePresentation =
  createToolHumanGuidancePresentation({
    translate: (...args) => typeof t === 'function' ? t(...args) : args[0],
    renderMarkdown: (source) => typeof renderMarkdown === 'function'
      ? renderMarkdown(source) : escapeHtml(source),
  });
var renderToolHumanGuidanceHtml =
  _testToolHumanGuidancePresentation.renderGuidanceHtml;
'''
        body = '\n'.join(
            path.read_text(encoding='utf-8') for path in owner_sources
        ) + composition + '\n' + body + (
            '\nvar ToolElapsedTickerTestPort = ToolElapsedTicker;\n'
        )
    if scope_prelude:
        body = (
            'var runtimeScope = typeof window !== "undefined" '
            '? window : globalThis;\n' + body)
    return body


def runtime_section_names() -> list[str]:
    names = [str(row['source']) for row in _manifest_rows()]
    for extra in RUNTIME_EXTRA:
        names.extend(re.findall(
            r'/\* ===== migrated source: (.+?) ===== \*/',
            extra.read_text(encoding='utf-8'),
        ))
    return names


def runtime_section_path(name: str, *, scope_prelude: bool = True) -> str:
    if name in _RETIRED_ORCHESTRATION_API_NAMES:
        _materialize_orchestration_api_legacy_graph()
        return str(_DIRECTORY / name)
    key = f'{name}:{scope_prelude}'
    cached = _CACHE.get(key)
    if cached is not None:
        return str(cached)
    body = runtime_section(name, scope_prelude=scope_prelude)
    if name == 'api.js' and scope_prelude:
        # Production imports the required transport statically. Older Node
        # fixtures still execute the endpoint-registry section as a classic
        # script, so materialize that same typed owner ahead of the section;
        # never resurrect the deleted in-registry transport fallback just to
        # keep an isolated test harness alive.
        transport_path = Path(native_module_path(
            '.native/api-transport.js',
            ROOT / 'frontend/src/api/transport.ts',
        ))
        body = (
            transport_path.read_text(encoding='utf-8')
            + '\nvar requiredApiTransport = globalThis.apiTransport;\n'
            + body
        )
    if scope_prelude:
        path = _DIRECTORY / name
    else:
        digest = hashlib.sha256(key.encode()).hexdigest()[:12]
        path = _DIRECTORY / '.raw' / f'{Path(name).stem}-{digest}.js'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    _CACHE[key] = path
    return str(path)


def runtime_sections_dir() -> str:
    """Materialize a logical-path test view of every migrated JS section."""
    for name in runtime_section_names():
        runtime_section_path(name)
    # Orchestration's retained transport consumes the typed immutable result
    # port that production imports through its generated lazy-runtime header.
    # Legacy raw-section harnesses need the same owner at their historical
    # logical path; never recreate a handwritten retained compatibility file.
    native_module_path(
        'api/http-result.js',
        ROOT / 'frontend/src/core/http-result.ts',
    )
    _materialize_orchestration_api_legacy_graph()
    return str(_DIRECTORY)


def native_module_path(name: str, source: str | Path) -> str:
    """Bundle one migrated TypeScript owner as an isolated classic test view.

    The production graph stays ESM.  Legacy jsdom fixtures can use this
    adapter while they are migrated: named exports are copied onto
    ``globalThis`` and registry-owned properties retain their descriptors, so
    live compatibility accessors behave like the composed runtime without
    recreating the deleted ``static/js`` source tree in the repository.
    """
    source_path = Path(source)
    if not source_path.is_absolute():
        source_path = ROOT / source_path
    key = f'native:{name}:{source_path}'
    cached = _CACHE.get(key)
    if cached is not None:
        return str(cached)
    test_bundler = ROOT / 'scripts' / 'vite_test_bundle.mjs'
    if not test_bundler.is_file():
        raise AssertionError(
            'vite_test_bundle.mjs is required to materialize native test modules')
    path = _DIRECTORY / name
    path.parent.mkdir(parents=True, exist_ok=True)
    global_name = 'TofuNativeTest_' + hashlib.sha256(key.encode()).hexdigest()[:12]
    compile_source = source_path
    footer = f'Object.assign(globalThis,{global_name});'
    orchestration_dir = ROOT / 'frontend' / 'src' / 'features' / 'orchestration'
    features_dir = ROOT / 'frontend' / 'src' / 'features'
    if source_path.parent == orchestration_dir:
        entry = _DIRECTORY / '.native-entry' / f'{global_name}.ts'
        entry.parent.mkdir(parents=True, exist_ok=True)
        registry = orchestration_dir / 'registry.ts'
        entry.write_text(
            f'import * as owner from {source_path.as_posix()!r};\n'
            f'import {{ orchestrationRegistry }} from {registry.as_posix()!r};\n'
            'export { owner, orchestrationRegistry };\n',
            encoding='utf-8',
        )
        compile_source = entry
        footer = (
            f'Object.assign(globalThis,{global_name}.owner);'
            'Object.defineProperties(globalThis,Object.getOwnPropertyDescriptors('
            f'{global_name}.orchestrationRegistry));'
            f'globalThis.orchestrationRegistry={global_name}.orchestrationRegistry;')
    elif features_dir in source_path.parents:
        entry = _DIRECTORY / '.native-entry' / f'{global_name}.ts'
        entry.parent.mkdir(parents=True, exist_ok=True)
        registry = ROOT / 'frontend' / 'src' / 'feature-registry.ts'
        entry.write_text(
            f'import * as owner from {source_path.as_posix()!r};\n'
            f'import {{ featureRegistry }} from {registry.as_posix()!r};\n'
            'export { owner, featureRegistry };\n',
            encoding='utf-8',
        )
        compile_source = entry
        footer = (
            f'Object.assign(globalThis,{global_name}.owner,'
            f'{global_name}.featureRegistry);')
    result = subprocess.run(
        [str(test_bundler), str(compile_source), '--bundle', '--format=iife',
         '--platform=browser', f'--global-name={global_name}',
         f'--footer:js={footer}',
         f'--outfile={path}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f'failed to compile native test module {source_path}:\n{result.stderr}')
    _CACHE[key] = path
    return str(path)


def _materialize_orchestration_api_legacy_graph() -> None:
    """Compile the retired classic API paths into one temporary typed view.

    Older Node fixtures still load the six historical files in order. The
    first path exposes the generated contract and typed transport adapters;
    the final path installs the stable facade after a fixture has created its
    ``Api.orchestrations`` placeholder. Nothing is written to the repository
    or shipped browser graph.
    """
    key = 'native:retired-orchestration-api-graph'
    if _CACHE.get(key) is not None:
        return
    test_bundler = ROOT / 'scripts' / 'vite_test_bundle.mjs'
    source = ROOT / 'frontend/src/features/orchestration/api-client.ts'
    contracts = (
        ROOT / 'frontend/src/features/orchestration/'
        'request-contracts.generated.ts'
    )
    entry = _DIRECTORY / '.native-entry/orchestration-api-legacy.ts'
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        f"import {{\n"
        "  createOrchestrationEndpointTransport,\n"
        "  installOrchestrationApiClient,\n"
        "  orchestrationEndpointContract,\n"
        "  orchestrationEndpointContracts,\n"
        "  resolveOrchestrationApiClient,\n"
        f"}} from {source.as_posix()!r};\n"
        "import { ORCHESTRATION_REQUEST_CONTRACTS } from "
        f"{contracts.as_posix()!r};\n"
        "const scope = globalThis as any;\n"
        "const endpointContracts = orchestrationEndpointContracts();\n"
        "const responseContracts: Record<string, any> = Object.create(null);\n"
        "const methodContracts: Record<string, any> = Object.create(null);\n"
        "for (const [name, contract] of Object.entries(endpointContracts)) {\n"
        "  responseContracts[contract.responseContract] ||= Object.freeze({\n"
        "    name: contract.responseContract, optionName: contract.optionName,\n"
        "    requiredFields: contract.responseRequiredFields,\n"
        "  });\n"
        "  methodContracts[name] = Object.freeze({\n"
        "    name, resultMethod: contract.resultMethod,\n"
        "    directMethod: contract.directMethod,\n"
        "  });\n"
        "}\n"
        "Object.freeze(responseContracts); Object.freeze(methodContracts);\n"
        "scope.ApiOrchestrationHttpContract = Object.freeze({\n"
        "  contract: orchestrationEndpointContract,\n"
        "  contracts: orchestrationEndpointContracts,\n"
        "});\n"
        "scope.ApiOrchestrationResponseContracts = Object.freeze({\n"
        "  contract: (name: string) => responseContracts[name] || null,\n"
        "  contracts: () => responseContracts,\n"
        "});\n"
        "scope.ApiOrchestrationClientMethods = Object.freeze({\n"
        "  contract: (name: string) => methodContracts[name] || null,\n"
        "  contracts: () => methodContracts,\n"
        "});\n"
        "scope.ApiOrchestrationEndpointTransport = Object.freeze({\n"
        "  create: createOrchestrationEndpointTransport,\n"
        "  resolveClient: resolveOrchestrationApiClient,\n"
        "});\n"
        "scope.ApiOrchestrationEndpoints = Object.freeze({\n"
        "  contract: orchestrationEndpointContract,\n"
        "  contracts: orchestrationEndpointContracts,\n"
        "  createTransport: createOrchestrationEndpointTransport,\n"
        "  resolveClient: resolveOrchestrationApiClient,\n"
        "});\n"
        "scope.resolveOrchestrationApiClient = resolveOrchestrationApiClient;\n"
        "if (scope.Api?.orchestrations) installOrchestrationApiClient(scope.Api);\n"
        "export { installOrchestrationApiClient, ORCHESTRATION_REQUEST_CONTRACTS };\n",
        encoding='utf-8',
    )
    first = _DIRECTORY / _RETIRED_ORCHESTRATION_API_NAMES[0]
    first.parent.mkdir(parents=True, exist_ok=True)
    global_name = 'TofuNativeOrchestrationApi'
    result = subprocess.run(
        [str(test_bundler), str(entry), '--bundle', '--format=iife',
         '--platform=browser', f'--global-name={global_name}',
         f'--footer:js=Object.assign(globalThis,{global_name});',
         f'--outfile={first}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f'failed to compile typed orchestration API test view:\n'
            f'{result.stderr}')
    for name in _RETIRED_ORCHESTRATION_API_NAMES[1:-1]:
        path = _DIRECTORY / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '/* typed orchestration API graph already loaded */\n',
            encoding='utf-8',
        )
    final = _DIRECTORY / _RETIRED_ORCHESTRATION_API_NAMES[-1]
    final.write_text(
        'if (typeof installOrchestrationApiClient === "function" '
        '&& globalThis.Api && globalThis.Api.orchestrations) '
        'installOrchestrationApiClient(globalThis.Api);\n',
        encoding='utf-8',
    )
    _CACHE[key] = first


def native_module_graph(
        entries: list[tuple[str, str | Path]],
        *,
        output_dir: str | Path | None = None) -> str:
    """Materialize several native owners as one shared classic-script graph.

    Some legacy fixtures still evaluate logical owners one file at a time.  A
    native ESM graph, however, has one module-private compatibility registry.
    Put the bundle at the first logical path and harmless placeholders at the
    remaining paths so those fixtures preserve their evaluation order without
    accidentally creating one registry per owner. Registry descriptors are
    projected intact so stateful getters and setters remain live.
    """
    if not entries:
        raise AssertionError('native module graph requires at least one entry')
    resolved = [
        (name, source if Path(source).is_absolute() else ROOT / source)
        for name, source in entries
    ]
    key = 'native-graph:' + '|'.join(
        f'{name}:{Path(source)}' for name, source in resolved)
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    if output_dir is not None:
        target_root = Path(output_dir)
    else:
        # Default to an isolated per-graph directory: placeholder stubs must
        # never overwrite real materialized sections in the shared directory.
        target_root = _DIRECTORY / '.graphs' / digest
    key += f'|out:{target_root}'
    cached = _CACHE.get(key)
    if cached is not None:
        return str(cached)
    test_bundler = ROOT / 'scripts' / 'vite_test_bundle.mjs'
    if not test_bundler.is_file():
        raise AssertionError(
            'vite_test_bundle.mjs is required to materialize native test modules')
    global_name = f'TofuNativeGraph_{digest}'
    entry = _DIRECTORY / '.native-entry' / f'{global_name}.ts'
    entry.parent.mkdir(parents=True, exist_ok=True)
    orchestration_dir = ROOT / 'frontend' / 'src' / 'features' / 'orchestration'
    imports = [
        f'import * as owner{index} from {Path(source).as_posix()!r};'
        for index, (_, source) in enumerate(resolved)
    ]
    imports.append(
        'import { orchestrationRegistry } from '
        f'{(orchestration_dir / "registry.ts").as_posix()!r};')
    exports = ', '.join(
        [f'owner{index}' for index in range(len(resolved))]
        + ['orchestrationRegistry'])
    entry.write_text(
        '\n'.join(imports) + f'\nexport {{ {exports} }};\n',
        encoding='utf-8',
    )
    first = target_root / resolved[0][0]
    first.parent.mkdir(parents=True, exist_ok=True)
    owners = ','.join(
        f'{global_name}.owner{index}' for index in range(len(resolved)))
    footer = (
        f'Object.assign(globalThis,{owners});'
        'Object.defineProperties(globalThis,Object.getOwnPropertyDescriptors('
        f'{global_name}.orchestrationRegistry));'
        f'globalThis.orchestrationRegistry={global_name}.orchestrationRegistry;')
    result = subprocess.run(
        [str(test_bundler), str(entry), '--bundle', '--format=iife',
         '--platform=browser', f'--global-name={global_name}',
         f'--footer:js={footer}', f'--outfile={first}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f'failed to compile native test graph:\n{result.stderr}')
    for name, _ in resolved[1:]:
        placeholder = target_root / name
        placeholder.parent.mkdir(parents=True, exist_ok=True)
        placeholder.write_text(
            '/* owner loaded by the shared native test graph */\n',
            encoding='utf-8',
        )
    _CACHE[key] = first
    return str(first)


def orchestration_legacy_test_root() -> str:
    """Return a temporary pre-Vite-shaped root backed by current owners.

    This exists solely for older Node fixtures whose harness code still joins
    ``ROOT/static/js``.  Retained sources are materialized from app-runtime;
    native orchestration entries are bundled in one bounded-concurrency Vite invocation.  No
    compatibility files are created in the repository or shipped bundle.
    """
    key = 'orchestration-legacy-test-root'
    if _CACHE.get(key) is not None:
        return str(_LEGACY_ROOT)
    runtime_sections_dir()
    test_bundler = ROOT / 'scripts' / 'vite_test_bundle.mjs'
    if not test_bundler.is_file():
        raise AssertionError(
            'vite_test_bundle.mjs is required to materialize native test modules')
    orchestration_dir = ROOT / 'frontend' / 'src' / 'features' / 'orchestration'
    entry_dir = _DIRECTORY / '.native-batch-entry'
    entry_dir.mkdir(parents=True, exist_ok=True)
    entries: list[Path] = []
    for source in sorted(orchestration_dir.glob('*.ts')):
        if source.name.endswith('.d.ts') or source.name == 'registry.ts':
            continue
        legacy_name = (f'{source.stem}.js'
                       if source.stem == 'task-mode'
                       or source.stem.startswith('task-mode-')
                       else f'orchestration-{source.stem}.js')
        if (_DIRECTORY / legacy_name).is_file():
            continue
        entry = entry_dir / (Path(legacy_name).stem + '.ts')
        entry.write_text(
            f'import * as owner from {source.as_posix()!r};\n'
            'import { orchestrationRegistry } from '
            f'{(orchestration_dir / "registry.ts").as_posix()!r};\n'
            'Object.assign(globalThis, owner, orchestrationRegistry);\n'
            '(globalThis as any).orchestrationRegistry = orchestrationRegistry;\n',
            encoding='utf-8',
        )
        entries.append(entry)
    if entries:
        result = subprocess.run(
            [str(test_bundler), *map(str, entries), '--bundle', '--format=iife',
             '--platform=browser', f'--outdir={_DIRECTORY}',
             '--log-level=warning'],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise AssertionError(
                f'failed to compile native orchestration test view:\n'
                f'{result.stderr}')
    static_dir = _LEGACY_ROOT / 'static'
    static_dir.mkdir(parents=True, exist_ok=True)
    js_link = static_dir / 'js'
    if not js_link.exists():
        js_link.symlink_to(_DIRECTORY, target_is_directory=True)
    modules_link = _LEGACY_ROOT / 'node_modules'
    if not modules_link.exists():
        modules_link.symlink_to(ROOT / 'node_modules', target_is_directory=True)
    for name in ('frontend', 'lib', 'scripts', 'templates'):
        source = ROOT / name
        target = _LEGACY_ROOT / name
        if source.exists() and not target.exists():
            target.symlink_to(source, target_is_directory=True)
    index_source = ROOT / 'index.html'
    index_target = _LEGACY_ROOT / 'index.html'
    if index_source.is_file() and not index_target.exists():
        index_target.symlink_to(index_source)
    repository_static = ROOT / 'static'
    if repository_static.is_dir():
        for source in repository_static.iterdir():
            if source.name == 'js':
                continue
            target = static_dir / source.name
            if not target.exists():
                target.symlink_to(source, target_is_directory=source.is_dir())
    _CACHE[key] = _LEGACY_ROOT
    return str(_LEGACY_ROOT)
