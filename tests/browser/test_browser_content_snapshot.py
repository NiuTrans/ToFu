"""Offline execution of the extension's actual snapshot functions (no accounts)."""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def test_content_snapshot_dom_fixtures():
    if not shutil.which('node'):
        pytest.skip('node is required for extension fixtures')
    source = (ROOT / 'browser_extension/background.js').read_text()
    capture = source[source.index('function _captureContentSnapshot('):source.index('function _researchPageSignals(')]
    fixture = r'''
const assert = require('node:assert/strict');
let visits = 0;
global.getComputedStyle = n => { visits++; return n.style || {display:'block', visibility:'visible'}; };
function text(value) { return {nodeType:3, nodeValue:value}; }
function el(tag, attrs={}, children=[]) {
  const n = {nodeType:1, tagName:tag.toUpperCase(), getAttribute:k=>attrs[k] || '',
    href:attrs.href || '', hidden:!!attrs.hidden, open:!!attrs.open};
  children.forEach((c,i)=>c.nextSibling=children[i+1] || null);
  n.firstChild=children[0] || null;
  return n;
}
function capture(body, cap=50000) {
  global.document={body, querySelector:()=>body};
  return _captureContentSnapshot(cap);
}
const signed='https://example.test/item?token=A%2FB&expires=123&sig=x%2By';
const card=el('article',{},[el('a',{href:signed},[text('Actual title')]),text('Useful snippet')]);
const host=el('div');
host.shadowRoot={firstChild:el('style',{},[text('.noise { color:red }')])};
host.shadowRoot.firstChild.nextSibling=el('pre',{},[text('const x = { color: "red" };')]);
const body=el('body',{},[el('nav',{},[text('Login'),el('a',{href:'https://example.test/account'},[text('Account')])]),
  el('main',{},[card,host,el('div',{hidden:true},[text('hidden noise')]),el('script',{},[text('script noise')])])]);
const result=capture(body);
assert.equal(result.records[0].url,signed);
assert.match(result.text,/const x =/);
assert.doesNotMatch(result.text,/noise|Account|Login/);
assert.equal(result.blocked,false);
assert.equal(body.firstChild.tagName,'NAV'); // traversal never removes nodes
assert.equal(capture(el('body',{},[text('Please sign in to continue')])).blocked,true);
assert.equal(capture(el('main',{},[text('No results found')])).empty,true);
assert.equal(capture(el('main',{},[text('An article about login security')])).blocked,false);
const cards=Array.from({length:9000},(_,i)=>el('li',{},[el('a',{href:'https://example.test/'+i},[text('Item '+i)])]));
visits=0;
const bounded=capture(el('main',{},cards),1000);
assert.ok(visits<=5000);
assert.ok(bounded.records.length<=40);
assert.ok(bounded.text.length+bounded.records.reduce((n,r)=>n+r.url.length+r.title.length+r.snippet.length,0)<=1000);
assert.equal(capture(el('main')).empty,false);
assert.equal(bounded.truncated,true);
const selected=el('section',{},[text('Wanted')]);
selected.nextSibling=el('main',{},[text('Unrelated confidential text')]);
global.document={body:selected,querySelector:()=>selected};
assert.equal(_captureContentSnapshot(50000,'#wanted').text,'Wanted');
const acc={lines:['first page'],seen:new Set(),chars:10};
_mergeResearchSnapshot(acc,result,50000);
const later=capture(el('main',{},[el('article',{},[el('a',{href:'https://example.test/virtual2'},[text('Later virtual item')])])]));
_mergeResearchSnapshot(acc,later,50000);
_mergeResearchSnapshot(acc,result,50000);
assert.equal(acc.snapshot.records.length,2);
assert.equal(acc.snapshot.records[1].url,'https://example.test/virtual2');
'''
    result = subprocess.run(['node', '-e', capture + fixture], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
