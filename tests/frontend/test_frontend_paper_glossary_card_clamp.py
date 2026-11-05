"""Glossary reveal stays within its reading column and retires with its host.

Drive the actual article lifecycle through mouse/focus events. A released
scope is the negative control: the same reveal no longer positions the card.
No private function lookup, source mutation or shared temporary scripts.
"""
from pathlib import Path
import json
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def article_owner():
    with compiled_typescript(
        ROOT / 'frontend/src/features/paper/report-article/index.ts',
    ) as bundle:
        yield bundle


def _reveal_card(bundle: str, *, release: bool) -> dict:
    harness = r"""
const fs = require('fs');
const { JSDOM } = require(process.argv[1] + '/node_modules/jsdom');
const win = new JSDOM('<!DOCTYPE html><body></body>', {url:'http://localhost/'}).window;
global.window = win; global.document = win.document;
global.Element = win.Element; global.AbortController = win.AbortController;
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));
const owner = global.TofuTestBundle;
function reveal(termLeft, type) {
  const host = document.createElement('section');
  host.className = 'paper-report-content';
  host.innerHTML = '<article><span class="paper-term" tabindex="0">Term' +
    '<span class="paper-term-card">Definition</span></span></article>';
  document.body.append(host);
  const article = host.querySelector('article'), term = host.querySelector('.paper-term');
  const card = host.querySelector('.paper-term-card');
  host.getBoundingClientRect = () => ({left:0,right:800,width:800});
  term.getBoundingClientRect = () => ({left:termLeft,right:termLeft+40,width:40});
  Object.defineProperty(card, 'offsetWidth', {value:320});
  owner.mountReportArticle(host, article, {openImage(){},reducedMotion:()=>true});
  if (process.argv[3] === 'release') owner.releaseReportArticle(host);
  term.dispatchEvent(new win.Event(type, {bubbles:true}));
  const left = termLeft + parseFloat(card.style.left || '0');
  owner.releaseReportArticle(host); host.remove();
  return {left,right:left+320};
}
console.log(JSON.stringify({mouse:reveal(700,'mouseover'),focus:reveal(700,'focusin'),
  left:reveal(20,'mouseover')}));
"""
    result = subprocess.run(
        ['node', '-e', harness, str(ROOT), bundle, 'release' if release else 'mounted'],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_glossary_card_clamped_into_column(article_owner):
    positions = _reveal_card(article_owner, release=False)
    for event in ('mouse', 'focus'):
        assert positions[event]['right'] <= 792
        assert positions[event]['left'] >= 8
    assert positions['left'] == {'left': 20, 'right': 340}


def test_nc_glossary_clamp_is_load_bearing(article_owner):
    positions = _reveal_card(article_owner, release=True)
    assert positions['mouse'] == {'left': 700, 'right': 1020}
    assert positions['focus'] == positions['mouse']
