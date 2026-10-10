"""Card-face regression for the outside-workspace edit refusal.

The model-facing `[N] FAIL` line keeps the full OutsideWorkspaceError sentence
(it carries the re-issue instructions); the projected card must instead render
one brief localized notice + gate badge, suppress the 110-char truncated-path
header chip, and replace the per-item verbose dump with a one-line brief. A
non-refusal failure keeps the established header chip and full detail.
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


def _ready() -> bool:
    return bool(shutil.which("node")) and BUNDLER.is_file()


pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not _ready(), reason="node + Vite test bundler required")]


@pytest.fixture(scope="module")
def presenters_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    work = tmp_path_factory.mktemp("outside-workspace-card")
    entry = work / "entry.ts"
    entry.write_text(f"""
import {{ createToolResultPresentation }} from {json.dumps(str(ROOT / 'frontend/src/conversation/presentation/tool-result-presentation.ts'))};
import {{ createWriteGateRefusalPresentation }} from {json.dumps(str(ROOT / 'frontend/src/conversation/presentation/write-gate-refusal.ts'))};

const translate:any = (key:string, values?:any) => {{
  let text = key;
  if (values && typeof values === 'object') text = text.replace(/\\{{(\\w+)\\}}/g, (_:string, n:string) => String(values[n] ?? `{{${{n}}}}`));
  return text;
}};
const iconHtml = (name:string) => `<i>${{name}}</i>`;
const gate = createWriteGateRefusalPresentation({{translate, iconHtml}});
const result = createToolResultPresentation({{translate, writeGateRefusal:gate}});
(globalThis as any).gate = gate;
(globalThis as any).renderWriteResultHtml = result.renderWriteResultHtml;
""", encoding="utf-8")
    built = work / "presenters.cjs"
    process = subprocess.run(
        [str(BUNDLER), str(entry), "--bundle", "--format=cjs", "--platform=node",
         f"--outfile={built}"], cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == 0, process.stderr
    return built


_REFUSAL_DETAIL = (
    "/mnt/x/probe-playback.py [replace]: Refusing to write to "
    "/mnt/x/probe-playback.py: it is outside every registered workspace root, "
    "and the write would auto-register \"/mnt/x\" as a new workspace root. If "
    "the user has explicitly confirmed they want this exact file written "
    "there, re-issue the same call with allow_outside_workspace=true. "
    "Otherwise write inside the project workspace instead.")


def _render(presenters_bundle: Path, refusal: bool) -> str:
    detail = _REFUSAL_DETAIL if refusal else 'a.py: anchor not found'
    runner = """
require(%s);
const detail = %s;
const toolContent = 'Applied 0/1 edits (1 failed)\\n[1] FAIL ' + detail;
const round = {
  toolName: 'edit_file', status: 'done', roundNum: '5',
  toolArgs: JSON.stringify({edits: [{path: '/mnt/x/probe-playback.py', operation: 'replace', anchor: 'a', content: 'b'}]}),
  toolContent,
};
const summary = {path: '/mnt/x/probe-playback.py', description: '', status: 'fail', detail};
if (%s) summary.outsideWorkspace = true;
const meta = {badge: '0/1 edits', writeOk: false, editSummaries: [summary]};
if (%s) meta.refusal = {kind: 'outside_workspace', paths: ['/mnt/x/probe-playback.py']};
const refusalInfo = globalThis.gate.resolveRefusal(round, meta);
const header = {iconHtml: 'I',
  queryHtml: 'Edit /mnt/x/probe-playback.py — make probe robust', rootPillHtml: '',
  badgeHtml: globalThis.gate.renderBadgeHtml(refusalInfo)};
console.log('===BADGE===');
console.log(header.badgeHtml);
console.log('===CARD===');
console.log(globalThis.renderWriteResultHtml(round, meta, header));
""" % (
        json.dumps(str(presenters_bundle)), json.dumps(detail),
        'true' if refusal else 'false', 'true' if refusal else 'false')
    process = subprocess.run(
        ["node", "-e", runner], cwd=ROOT, capture_output=True, text=True,
        timeout=60)
    assert process.returncode == 0, process.stderr
    return process.stdout


def test_outside_workspace_card_is_brief(presenters_bundle: Path):
    out = _render(presenters_bundle, refusal=True)
    badge, _, card = out.partition("===CARD===")
    assert 'ptool-badge-gate' in badge
    assert 'tool.gateOutsideWorkspaceBadge' in badge
    # One brief notice under the header — never the verbose model-facing dump.
    assert 'ptool-gate-note' in card
    assert 'tool.gateOutsideWorkspaceTitle' in card
    assert 'probe-playback.py' in card  # path chip keeps the target visible
    # The useless truncated-path chip is gone (header AND per-item).
    assert 'ptool-batch-fail-reason' not in card
    # Per-item detail is the one-line brief, not the instruction sentence.
    assert 'tool.outsideWorkspaceItem' in card
    fail_detail = card.split('ptool-batch-fail-detail', 1)[1]
    assert 'auto-register' not in fail_detail.split('</code>', 1)[0]


def test_ordinary_failure_keeps_header_chip_and_full_detail(
        presenters_bundle: Path):
    out = _render(presenters_bundle, refusal=False)
    badge, _, card = out.partition("===CARD===")
    assert 'ptool-badge-gate' not in badge
    assert 'ptool-gate-note' not in card
    assert 'ptool-batch-fail-reason' in card
    assert 'anchor not found' in card
