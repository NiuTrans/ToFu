/** Differential checks against installed Marked and the actual strict policy owner. */
import assert from 'node:assert/strict';
import {Marked} from '../../node_modules/marked/lib/marked.esm.js';
import {build} from '../../node_modules/esbuild/lib/main.js';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import './inline_cache.js';

const directory=await mkdtemp(join(tmpdir(),'tofu-inline-policy-'));
try {
  const destination=join(directory,'policy.mjs');
  await build({entryPoints:[new URL('../../frontend/src/markdown-policy.ts',import.meta.url).pathname],
    bundle:true,format:'esm',platform:'node',outfile:destination});
  const {installMarkdownPolicy}=await import(pathToFileURL(destination));
  const reference=new Marked(),candidate=new Marked();
  assert(installMarkdownPolicy(reference));assert(installMarkdownPolicy(candidate));
  let epoch=0;
  const parser=createInlineCacheExperiment({ownerId:900001,maximumBytes:1024*1024,getEpoch:()=>epoch,
    createLexer:()=>new candidate.Lexer(candidate.defaults),renderTokens:tokens=>candidate.parser(tokens)});
  const fixtures=['plain 中文','~~strict~~ ~literal~','[ref]\n\nTail.',
    '[ref]\n\nTail.\n\n[ref]: /first "one"','[ref]\n\nTail.\n\n[ref]: /second "two"',
    '- [ ] open\n- [x] done\n\n  continuation\n','> quote **bold**\n> nested [ref]\n\n[ref]: /source',
    '```js\nconst x="<script>";\n','```js\nconst x=1;\n```\n\nnext',
    '| name | value |\n| --- | --- |\n| a | 82\\|181 |',
    '<script>alert(1)</script><img src=x onerror=alert(1)>','**unfinished','[unfinished](',
    'before <pre>raw\n**literal**\n</pre> after','\\[escape\\] and `~~literal~~`',
    '- outer\n  - nested\n\n# Heading\n\n---\n','line\r\nnext\rline'];
  let comparisons=0;
  for (let cycle=0;cycle<3;cycle++) for (const source of fixtures) {
    assert.equal(parser.parse(900001,source),reference.parse(source));comparisons++;
  }
  for (const source of fixtures) for (let end=1;end<=source.length;end++) {
    assert.equal(parser.parse(900001,source.slice(0,end)),reference.parse(source.slice(0,end)));comparisons++;
  }
  let seed=20261008;
  const pieces=['**bold**','[ref]','\n\n','\n- [x] task\n','<pre>literal</pre>',
    '\n\n[ref]: /source\n','~~strict~~','\n> quote\n','中文 `code`','[ref]: /changed "title"\n'];
  for (let index=0;index<1000;index++) {
    let text='';
    for (let part=0;part<8;part++) {
      seed=(Math.imul(seed,1664525)+1013904223)>>>0;text+=pieces[seed%pieces.length];
    }
    assert.equal(parser.parse(900001,text),reference.parse(text));comparisons++;
  }
  const source='same **bold** paragraph\n\n'.repeat(100);
  assert.equal(parser.parse(900001,source),reference.parse(source));
  assert.equal(parser.parse(900001,source),reference.parse(source));
  assert(parser.snapshot(900001).hits>0);
  const retained=parser.snapshot(900001).retainedBytes;
  assert.throws(()=>parser.parse(900002,source),/Foreign/);
  assert.equal(parser.snapshot(900001).retainedBytes,retained);
  reference.setOptions({breaks:false});candidate.setOptions({breaks:false});epoch++;
  assert.equal(parser.parse(900001,'one\ntwo'),reference.parse('one\ntwo'));
  assert(parser.snapshot(900001).retainedBytes<=1024*1024);
  const tiny=createInlineCacheExperiment({ownerId:900001,maximumBytes:700,getEpoch:()=>epoch,
    createLexer:()=>new candidate.Lexer(candidate.defaults),renderTokens:tokens=>candidate.parser(tokens)});
  for (let index=0;index<30;index++) {
    const text=`evict **paragraph ${index}**`;
    assert.equal(tiny.parse(900001,text),reference.parse(text));
    assert(tiny.snapshot(900001).retainedBytes<=700);
  }
  tiny.dispose(900001);assert.equal(tiny.snapshot(900001).retainedBytes,0);
  parser.dispose(900001);assert.equal(parser.snapshot(900001).retainedBytes,0);
  assert.throws(()=>parser.parse(900001,source),/closed/);
  console.log(`${comparisons} exact growing-prefix/late-reference/strict-policy/task-list/state checks; ownership, epoch, bound and disposal pass. No browser/performance admission.`);
} finally { await rm(directory,{recursive:true,force:true}); }
