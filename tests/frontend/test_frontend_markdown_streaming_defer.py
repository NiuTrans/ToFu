"""Streaming render deferral: hljs/KaTeX skip per frame, restore at terminal.

The conversation streaming hot path re-renders the full accumulated markdown
on every delta frame. Syntax highlighting (highlightAuto on a growing code
block) and KaTeX on partial formulas are the dominant per-frame costs, and
they run on the same main thread that measures push RTT — so they
manufactured spurious high-latency readings and false offline verdicts
(2026-09-18 incident). Renderers now pass ``options.streaming`` for
non-terminal blocks; renderMarkdown must then skip hljs + KaTeX and must NOT
cache the degraded output, so the terminal render restores both.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path, runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
JS_DIR = runtime_sections_dir()
HTML_SAFETY = native_module_path(
    '.native/markdown-html-safety.js',
    os.path.join(ROOT, 'frontend', 'src', 'html-safety.ts'),
)

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const hljsCalls = { highlight: 0, highlightAuto: 0 };
const katexCalls = { count: 0 };
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body></body>',
  targets: [process.argv[2], process.argv[4]],
  globals: {
    marked: require(process.argv[3] + '/node_modules/marked/lib/marked.cjs'),
    BASE_PATH: '',
    projectState: {},
    _ensureKatex: function () {},
    hljs: {
      getLanguage: function () { return true; },
      highlight: function (text) { hljsCalls.highlight++; return { value: text }; },
      highlightAuto: function (text) { hljsCalls.highlightAuto++; return { value: text }; },
    },
    katex: {
      renderToString: function (tex) {
        katexCalls.count++;
        return '<span class="katex">' + tex + '</span>';
      },
    },
  },
});

const md = 'before\n\n```js\nconst x = 1;\n```\n\ninline $E = mc^2$ math';

// 1. Streaming render: no hljs, no KaTeX; math falls back to a pending span.
const streaming = renderMarkdown(md, { streaming: true });
check('stream_skips_hljs', hljsCalls.highlight === 0 && hljsCalls.highlightAuto === 0);
check('stream_skips_katex', katexCalls.count === 0);
check('stream_math_pending', /math-pending/.test(streaming));
check('stream_code_present', streaming.includes('const x = 1;'));

// 2. Terminal render of the SAME text must not reuse the degraded output:
//    hljs and KaTeX both run, and the result differs from the streaming one.
const terminal = renderMarkdown(md);
check('terminal_runs_hljs', hljsCalls.highlight > 0 || hljsCalls.highlightAuto > 0);
check('terminal_runs_katex', katexCalls.count > 0);
check('terminal_math_typeset', /katex/.test(terminal));
check('terminal_not_streaming_cache_hit', terminal !== streaming);

// 3. A second terminal render IS a cache hit (no extra hljs/KaTeX work).
const hlAfter = hljsCalls.highlight + hljsCalls.highlightAuto;
const katexAfter = katexCalls.count;
const terminal2 = renderMarkdown(md);
check('terminal_cache_hit', terminal2 === terminal
  && hljsCalls.highlight + hljsCalls.highlightAuto === hlAfter
  && katexCalls.count === katexAfter);

report();
"""


def test_streaming_render_defers_hljs_and_katex_until_terminal():
    run_harness(
        target_js=HTML_SAFETY,
        body_js=_BODY,
        extra_targets=[os.path.join(JS_DIR, 'core', 'markdown.js')],
        min_pass=9,
    )
