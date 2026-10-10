"""Streaming reconciliation preserves canonical DOM and stable reader nodes."""
import json
import subprocess

import pytest

from tests._runtime_sections import ROOT, native_module_path

pytestmark = pytest.mark.unit


def test_streaming_tail_matches_full_dom_replacement_and_retains_prefix():
    bundle = native_module_path('.native/streaming-markdown-dom.js',
                                ROOT / 'frontend/src/conversation/ui/renderers/stable-dom.ts')
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<!doctype html><body></body>',{runScripts:'outside-only'});
const w=dom.window;w.eval(fs.readFileSync(BUNDLE,'utf8'));
function check(before,after,retain=false){
  const actual=w.document.createElement('div'),expected=w.document.createElement('div');
  w.setManagedHtml(actual,before);const prefix=actual.firstElementChild;
  w.setStreamingMarkdownHtml(actual,after);expected.innerHTML=after;
  assert.equal(actual.innerHTML,expected.innerHTML,JSON.stringify({before,after}));
  if(retain)assert.equal(actual.firstElementChild,prefix);
  assert.equal(w.setStreamingMarkdownHtml(actual,after),false);
}
check('<p>stable</p>\n<p>tail</p>\n','<p>stable</p>\n<p>tail appended</p>\n',true);
check('<p>stable</p>\n<pre><code>a</code></pre>\n','<p>stable</p>\n<pre><code>a\nb</code></pre>\n',true);
check('<p>stable</p>\n<p>tail</p>\n','<p>stable</p>\n<h2>tail</h2>\n<p>next</p>\n',true);
check('<p>stable</p><p>tail</p>','<p>edited</p><p>tail appended</p>');
check('<p>[reference]</p><p>tail</p>','<p><a href="/reference">reference</a></p><p>tail appended</p>');
check('<p>stable</p><p id="old">tail</p>','<p>stable</p><p id="new">tail appended</p>',true);
check('<p>stable</p><p>tail</p>','<p>stable</p><p class="new">tail appended</p>',true);
const prefixes=['<p>stable</p>\n','<blockquote><p>quote</p></blockquote>\n',
  '<table><tbody><tr><td>cell</td></tr></tbody></table>\n','<p><b>broken</p>\n',
  '<div>unclosed<p>stable</p>\n','<svg><text>svg</text></svg>\n',
  '<p><a href="/a">link</p>\n','<p>中文 &amp; escaped</p>\n'];
const tails=['<p>tail</p>\n','<p>tail <em>rich</em></p>\n','<pre><code>code</code></pre>\n',
  '<p>tail<br>line</p>\n','<p id="tail">tail</p>\n','<p><b>tail</p>\n',
  '<p>tail</p><!--comment-->','<p>tail</p> non-whitespace',
  '<p>tail<table><tr><td>x</td></tr></table></p>', '<p>tail</p><p>tail</p>'];
for(const prefix of prefixes)for(const before of tails)for(const after of tails){
  check(prefix+before,prefix+after);
}
check('<p>stable</p><p>rich</p><p>later</p>',
      '<p>stable</p><pre><code>changed</code></pre><p>end</p>',true);
check('<p>stable</p><table><tbody><tr><td>a</td></tr></tbody></table>',
      '<p>stable</p><table><tbody><tr><td>b</td></tr></tbody></table>',true);
const actual=w.document.createElement('div');w.document.body.append(actual);
w.setManagedHtml(actual,'<p>stable</p>\n<p>tail</p>\n');
const tail=actual.lastElementChild.firstChild;
const selection=w.getSelection(),range=w.document.createRange();
range.setStart(tail,0);range.setEnd(tail,4);selection.addRange(range);
w.setStreamingMarkdownHtml(actual,'<p>stable</p>\n<p>tail appended</p>\n');
assert.equal(actual.lastElementChild.firstChild,tail);
assert.equal(selection.toString(),'tail');
const single=w.document.createElement('div');w.document.body.append(single);
w.setManagedHtml(single,'<p>one paragraph</p>');
const singleText=single.firstChild.firstChild;
selection.removeAllRanges();const singleRange=w.document.createRange();
singleRange.setStart(singleText,0);singleRange.setEnd(singleText,3);selection.addRange(singleRange);
w.setStreamingMarkdownHtml(single,'<p>one paragraph grows</p>');
assert.equal(single.firstChild.firstChild,singleText);
assert.equal(selection.toString(),'one');
const completed=w.document.createElement('div');w.document.body.append(completed);
w.setManagedHtml(completed,'<p>reader <strong>stable</strong></p>\n<pre><code>raw</code></pre>');
const reader=completed.firstElementChild;
selection.removeAllRanges();const readerRange=w.document.createRange();
readerRange.selectNodeContents(reader);selection.addRange(readerRange);
const highlighted='<p>reader <strong>stable</strong></p>\n<pre><code><span class="hljs-keyword">raw</span></code></pre>';
assert.equal(w.setCompletedMarkdownHtml(completed,highlighted),true);
assert.equal(completed.firstElementChild,reader);
assert.equal(selection.toString(),'reader stable');
assert.equal(completed.innerHTML,highlighted);
assert.equal(w.setCompletedMarkdownHtml(completed,highlighted),false);
w.setCompletedMarkdownHtml(completed,'<p>edited earlier</p><pre><code>raw</code></pre>');
assert.notEqual(completed.firstElementChild,reader);
dom.window.close();
""".replace('BUNDLE', json.dumps(bundle))
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True,
                            text=True, timeout=90)
    assert result.returncode == 0, result.stderr
