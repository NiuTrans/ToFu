"""Outcome regression for canonical Goal tool rounds on the real rich surface.

This is intentionally isolated from the broad ConversationSurface suite.  The
Vite entry composes the shipped typed presenters, while the retained tool-round
harness proves the final HTML dispatcher/debug-anchor integration.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
BUNDLER = ROOT / "scripts" / "vite_test_bundle.mjs"
CORE = ROOT / "frontend/src/runtime/sections/ui/tool_rounds.js"
RICH = ROOT / "frontend/src/runtime/sections/ui/tool_rounds_rich.js"


def _ready() -> bool:
    if not shutil.which("node") or not BUNDLER.is_file():
        return False
    return subprocess.run(
        [shutil.which("node"), "-e", "require('jsdom')"], cwd=ROOT,
        capture_output=True, text=True,
    ).returncode == 0


pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not _ready(), reason="node + jsdom + Vite test bundler required")]


@pytest.fixture(scope="module")
def goal_outcome_harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    work = tmp_path_factory.mktemp("goal-round-outcomes")
    entry = work / "entry.ts"
    entry.write_text(f"""
import {{ renderChecklistCard }} from {json.dumps(str(ROOT / 'frontend/src/conversation/ui/checklist-card/index.ts'))};
(globalThis as any).renderChecklistCard = renderChecklistCard;
import {{ renderMcpResourceLinks }} from {json.dumps(str(ROOT / 'frontend/src/conversation/presentation/mcp-resource-links.ts'))};
(globalThis as any).renderMcpResourceLinks = renderMcpResourceLinks;
import {{ createToolGroupPresentation }} from {json.dumps(str(ROOT / 'frontend/src/conversation/ui/tool-groups/index.ts'))};
(globalThis as any).createToolGroupPresentation = createToolGroupPresentation;
import {{ createToolHeaderPresentation }} from {json.dumps(str(ROOT / 'frontend/src/conversation/presentation/tool-header-presentation.ts'))};
import {{ createToolResultPresentation }} from {json.dumps(str(ROOT / 'frontend/src/conversation/presentation/tool-result-presentation.ts'))};
import {{ createToolImagePresentation }} from {json.dumps(str(ROOT / 'frontend/src/conversation/presentation/tool-image-presentation.ts'))};
import {{ createToolCommandExecutionPresentation }} from {json.dumps(str(ROOT / 'frontend/src/conversation/presentation/tool-command-execution-presentation.ts'))};

import {{ EXPLICIT_TOOL_ROUND_DISPLAY_NAMES, explicitToolRoundDisplay, toolRoundDisplay, isProjectToolRound, isBrowserToolRound }} from {json.dumps(str(ROOT / 'frontend/src/conversation/presentation/tool-round-presentation.ts'))};
import {{ TOOL_ROUND_CHEVRON_RIGHT_SVG, toolRoundSvg }} from {json.dumps(str(ROOT / 'frontend/src/conversation/presentation/tool-round-icons.ts'))};
import {{ createWriteGateRefusalPresentation }} from {json.dumps(str(ROOT / 'frontend/src/conversation/presentation/write-gate-refusal.ts'))};
import {{ escapeHtmlText }} from {json.dumps(str(ROOT / 'frontend/src/html-safety.ts'))};
import {{ inspectorReference }} from {json.dumps(str(ROOT / 'frontend/src/features/inspector-evidence.ts'))};

const translate:any = (key:string, values?:any) => {{
  const copy:any = {{
    'toolCommandExecution.failed':'failed', 'toolCmd.finished':'finished',
    'toolCmd.foldCommand':'Command', 'toolCmd.foldOutput':'Output',
    'toolImage.images':'{{n}} images', 'inspect.opsTitle':'Applied transform',
    'inspect.cropped':'cropped', 'inspect.opsSep':', ',
  }};
  let text = copy[key] || key;
  if (values && typeof values === 'object') text = text.replace(/\\{{(\\w+)\\}}/g, (_:string, n:string) => String(values[n] ?? `{{${{n}}}}`));
  return text;
}};
const iconHtml = (name:string) => `<i>${{escapeHtmlText(name)}}</i>`;
const gate = createWriteGateRefusalPresentation({{translate, iconHtml}});

const header = createToolHeaderPresentation({{translate, writeGateRefusal:gate}});
const result = createToolResultPresentation({{translate, writeGateRefusal:gate}});
const image = createToolImagePresentation({{translate, iconHtml}});
const command = createToolCommandExecutionPresentation({{translate}});
(globalThis as any).EXPLICIT_TOOL_ROUND_DISPLAY_NAMES = EXPLICIT_TOOL_ROUND_DISPLAY_NAMES;
(globalThis as any).explicitToolRoundDisplay = explicitToolRoundDisplay;

(globalThis as any).toolRoundDisplay = toolRoundDisplay;

(globalThis as any).isProjectToolRound = isProjectToolRound;
(globalThis as any).isBrowserToolRound = isBrowserToolRound;
(globalThis as any).TOOL_ROUND_CHEVRON_RIGHT_SVG = TOOL_ROUND_CHEVRON_RIGHT_SVG;

(globalThis as any).toolRoundSvg = toolRoundSvg;
(globalThis as any).renderToolResultCompactionLabelHtml = result.renderCompactionLabelHtml;
(globalThis as any).renderGenericToolResultHtml = result.renderGenericResultHtml;
(globalThis as any).renderWriteToolResultHtml = result.renderWriteResultHtml;
(globalThis as any).renderToolImageHtml = image.renderImageHtml;
(globalThis as any).renderRunningToolCommandHtml = command.renderRunningCommandHtml;
(globalThis as any).renderSettledToolCommandHtml = command.renderSettledCommandHtml;
(globalThis as any).toolHeaderQueryText = header.queryText;

(globalThis as any).renderToolArtifactHeaderHtml = header.artifactHeaderHtml;
(globalThis as any).renderRepoMapHeaderHtml = header.repoMapHeaderHtml;
(globalThis as any).renderToolHeaderBadgeHtml = header.renderBadgeHtml;
// tool_rounds.js calls inspectorReference (bundle-resolved via _prelude.js
// since the 2026-09 squash merges); expose the REAL evidence routing.
(globalThis as any).inspectorReference = inspectorReference;
// The composed runtime declares runtimeScope before any eager section (the
// app-runtime prelude); bare section harnesses must mirror that or the
// first unguarded read (todo rail sync) throws ReferenceError.
(globalThis as any).runtimeScope = {{}};
""", encoding="utf-8")
    built = work / "presenters.cjs"
    process = subprocess.run(
        [str(BUNDLER), str(entry), "--bundle", "--format=cjs", "--platform=node",
         f"--outfile={built}"], cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == 0, process.stderr
    return built


def test_canonical_goal_rounds_render_real_rich_outcomes_and_agent_debug_anchor(
        goal_outcome_harness: Path, tmp_path: Path):
    harness = ROOT / ".tmp_goal_round_outcomes_harness.cjs"
    harness.write_text(r"""
const fs = require('fs');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<main></main>');
const runtimeScope = globalThis;
global.window = dom.window; global.document = dom.window.document;
global.Element = dom.window.Element;
global.escapeHtml = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (key, values) => {
  const copy = {'todo.head':'Checklist','toolCommandExecution.failed':'failed',
    'toolCmd.finished':'finished','toolCmd.foldCommand':'Command','toolCmd.foldOutput':'Output',
    'toolImage.images':'{n} images','inspect.opsTitle':'Applied transform',
    'inspect.cropped':'cropped','inspect.opsSep':', '};
  let text = copy[key] || key;
  if (values && typeof values === 'object') text = text.replace(/\{(\w+)\}/g, (_, n) => String(values[n] ?? `{${n}}`));
  return text;
};
global.Icon = n => `<i>${global.escapeHtml(n)}</i>`;
global.renderMarkdown = s => s; global._shortUrl = s => s; global.formatNumber = String;
global._featureFlags = {debug_mode:true};

global.handleToolExecutionDisclosureClick = () => {};

global.createDemandScopedPresentationTicker = () => ({start(){}, stop(){}});

global.renderToolInjectionHtml = () => '';
global.renderToolBrowserExecutionHtml = () => '';
global.renderToolSearchHtml = () => '';

global.renderToolHumanGuidanceHtml = () => '';

global.renderToolApprovalHtml = () => '';
require(process.argv[2]);
eval(fs.readFileSync(process.argv[3], 'utf8'));
eval(fs.readFileSync(process.argv[4], 'utf8'));
const base = {status:'done', _taskId:'goal-parent', agentId:'agent-7', llmRound:4, roundNum:4};
const rounds = [
  {...base, toolName:'todo_write', query:'Update plan', results:[{todos:[{content:'Ship canonical projection',status:'in_progress'}]}]},
  {...base, toolName:'write_file', query:'Write src/goal.ts', toolArgs:JSON.stringify({content:'export const GOAL_SENTINEL = 42;'}), results:[{badge:'1 line'}]},
  {...base, toolName:'apply_diff', query:'Edit src/goal.ts', toolArgs:JSON.stringify({search:'const OLD = 0;',replace:'const NEW = 1;'}), results:[{badge:'+1/-1'}]},
  {...base, toolName:'run_command', query:'node goal.js', results:[{command:'node goal.js',output:'GOAL COMMAND OUTPUT',exitCode:7,description:'verify goal'}]},
  {...base, toolName:'inspect_image', query:'Inspect goal-diagram.png', results:[{imageDataUris:[{uri:'data:image/png;base64,AA',filename:'goal-diagram.png'}],inspectOps:[{type:'crop'}]}]},
];
const html = rounds.map(r => _renderUnifiedToolLine(r, false));
const root = document.querySelector('main'); root.innerHTML = html.join('');
const anchor = root.querySelector('.ri-tool-anchor');
console.log(JSON.stringify({
  todo:root.querySelector('.ptool-todo-text')?.textContent,
  write:root.textContent.includes('GOAL_SENTINEL'),
  editOld:root.textContent.includes('const OLD = 0;'), editNew:root.textContent.includes('const NEW = 1;'),
  command:root.textContent.includes('node goal.js'), output:root.textContent.includes('GOAL COMMAND OUTPUT'),
  exit:root.textContent.includes('failed'), image:root.textContent.includes('goal-diagram.png'),
  anchorState:anchor?.getAttribute('data-ri-state'),
  anchorLabel:anchor?.querySelector('.ri-tool-anchor-label')?.textContent,
}));
""", encoding="utf-8")
    try:
        process = subprocess.run(
            [shutil.which("node"), str(harness), str(goal_outcome_harness),
             str(CORE), str(RICH)], cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        assert process.returncode == 0, process.stderr
        outcome = json.loads(process.stdout.strip().splitlines()[-1])
    finally:
        harness.unlink(missing_ok=True)
    assert outcome == {
        "todo": "Ship canonical projection", "write": True,
        "editOld": True, "editNew": True, "command": True, "output": True,
        "exit": True, "image": True,
        "anchorState": "goal-parent#agent:agent-7:4", "anchorLabel": "R4",
    }
