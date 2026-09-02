/**
 * Lazy highlight.js grammars, bundled as ONE shared chunk.
 *
 * Splitting each grammar into its own dynamic entry cost ~1 KiB of module
 * wrapper per chunk and defeated gzip's cross-grammar dictionary (+10 KiB
 * total). A single chunk restores the dictionary; the first exotic code block
 * downloads it once and every later grammar registers from the module cache.
 */
import hljsC from 'highlight.js/lib/languages/c';
import hljsCpp from 'highlight.js/lib/languages/cpp';
import hljsCsharp from 'highlight.js/lib/languages/csharp';
import hljsGo from 'highlight.js/lib/languages/go';
import hljsGraphql from 'highlight.js/lib/languages/graphql';
import hljsJava from 'highlight.js/lib/languages/java';
import hljsKotlin from 'highlight.js/lib/languages/kotlin';
import hljsLess from 'highlight.js/lib/languages/less';
import hljsLua from 'highlight.js/lib/languages/lua';
import hljsMakefile from 'highlight.js/lib/languages/makefile';
import hljsObjectivec from 'highlight.js/lib/languages/objectivec';
import hljsPerl from 'highlight.js/lib/languages/perl';
import hljsPhp from 'highlight.js/lib/languages/php';
import hljsPhpTemplate from 'highlight.js/lib/languages/php-template';
import hljsPythonRepl from 'highlight.js/lib/languages/python-repl';
import hljsR from 'highlight.js/lib/languages/r';
import hljsRuby from 'highlight.js/lib/languages/ruby';
import hljsRust from 'highlight.js/lib/languages/rust';
import hljsScala from 'highlight.js/lib/languages/scala';
import hljsScss from 'highlight.js/lib/languages/scss';
import hljsSwift from 'highlight.js/lib/languages/swift';
import hljsVbnet from 'highlight.js/lib/languages/vbnet';
import hljsWasm from 'highlight.js/lib/languages/wasm';

export const lazyHljsGrammars = {
  c: hljsC,
  cpp: hljsCpp,
  csharp: hljsCsharp,
  go: hljsGo,
  graphql: hljsGraphql,
  java: hljsJava,
  kotlin: hljsKotlin,
  less: hljsLess,
  lua: hljsLua,
  makefile: hljsMakefile,
  objectivec: hljsObjectivec,
  perl: hljsPerl,
  php: hljsPhp,
  'php-template': hljsPhpTemplate,
  'python-repl': hljsPythonRepl,
  r: hljsR,
  ruby: hljsRuby,
  rust: hljsRust,
  scala: hljsScala,
  scss: hljsScss,
  swift: hljsSwift,
  vbnet: hljsVbnet,
  wasm: hljsWasm,
} as const;

export type LazyHljsLanguage = keyof typeof lazyHljsGrammars;
