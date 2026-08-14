/* Static undefined-symbol scanner for the Tofu frontend bundle.
 *
 * Bug class (epic pt_fb854394c1f34eea): a JS symbol REFERENCED but never
 * DEFINED ships as a live browser ReferenceError — invisible to node --check
 * (syntax only) and to jsdom harnesses (which pre-inject mock globals and
 * never execute setTimeout/callback bodies). Root case: ff7176dd retired
 * streamBufs, left 7 `dBuf` reads → uncaught ReferenceError on every refresh
 * onto a generating conv (fixed 90ddbb96).
 *
 * Method: REAL parser (TypeScript compiler API — a dev-dependency, not a
 * hand-rolled regex; the regex prototype broke on regex literals and could
 * not do scope analysis). Per file:
 *   pass 1 — collect top-level declarations of EVERY scanned file into one
 *            global union (browser <script> semantics: all bundle files share
 *            the global scope), plus window.X / globalThis.X assignment
 *            targets anywhere in the file.
 *   pass 2 — walk each AST with a scope stack (global → function → catch;
 *            blocks intentionally do NOT scope — see APPROXIMATIONS), resolve
 *            every identifier READ against the chain, then the union, then
 *            the externals table (TS lib.dom + es* globals + vendor libs).
 *
 * APPROXIMATIONS (deliberate — they over-ACCEPT, i.e. false negatives on
 * OTHER bug classes, never false positives on ours):
 *   - let/const/class are hoisted to function scope (block-scope escapes and
 *     TDZ use-before-declare are a different bug class — tsc territory).
 *   - A bare assignment `X = …` to an undeclared name is treated as a
 *     sloppy-mode global DECLARATION and reported separately in `sloppy`
 *     (it is legal in non-strict scripts — and a real find worth listing).
 *   - `typeof X` probes are not reads: an identifier referenced ONLY under
 *     typeof is an optional external and is reported in `probed`, not
 *     `violations`. A non-typeof read of the same name still flags UNLESS a
 *     dominating guard makes it safe (see below).
 *   - typeof-guards are honoured as SCOPES: in `if (typeof X !== 'undefined')
 *     {…}`, `typeof X === 'function' ? … : …`, and `typeof X !== 'undefined'
 *     && X.go()` the guarded branch may read X freely (the canonical
 *     optional-global idiom). The guard set flows down the scope chain.
 *   - window.X READS only flag when the access can actually THROW — a plain
 *     `window.missing` read yields undefined (safe: feature probes, server-
 *     injected config); `window.missing.deep` / `window.missing()` throws a
 *     TypeError and flags. (A bare `missing` read ALWAYS throws — that is
 *     the dBuf class this gate exists for.)
 *   - IIFE window-aliases are recognised: `(function (global) { … })(typeof
 *     window !== 'undefined' ? window : this)` makes `global.X = …` a global
 *     declaration and `global.X` a global read (the api.js UMD idiom).
 *
 * Usage: node tests/_undef_scan.js <repo_root> <spec.json>
 *   spec.json: {"files": ["static/js/i18n.js", ...],
 *               "html":  ["index.html"],           // inline <script> blocks
 *               "extraGlobals": ["hljs", ...]}
 * Stdout JSON: {violations: [{file, line, col, name, context}],
 *               sloppy:     [{file, line, col, name}],
 *               probed:     [{file, name}],
 *               stats:      {files, globals, externals, refs, ms}}
 * Exit code: 0 always (the pytest layer asserts on the JSON); 2 on usage /
 * unreadable input so harness errors are never confused with findings.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
const SPEC_PATH = process.argv[3];
if (!ROOT || !SPEC_PATH) {
  console.error('usage: node _undef_scan.js <repo_root> <spec.json>');
  process.exit(2);
}
const ts = require(path.join(ROOT, 'node_modules', 'typescript'));

const SK = ts.SyntaxKind;

// ── Externals: globals declared by TypeScript's own lib.*.d.ts ─────────
// Version-accurate browser + ECMAScript globals (window, document, fetch,
// Promise, Intl, …) with zero hand-curation. Webworker/scripthost libs are
// excluded (worker-only names like importScripts are not window globals).
function loadTsLibGlobals() {
  const libDir = path.join(ROOT, 'node_modules', 'typescript', 'lib');
  const names = new Set();
  const re = /declare\s+(?:var|const|let|function|class|namespace)\s+([A-Za-z_$][\w$]*)/g;
  for (const f of fs.readdirSync(libDir)) {
    if (!/^lib\.(dom|es|decorators)/.test(f) || !f.endsWith('.d.ts')) continue;
    const text = fs.readFileSync(path.join(libDir, f), 'utf8');
    let m;
    while ((m = re.exec(text))) names.add(m[1]);
  }
  // Not all of these appear as `declare var` in every lib version.
  for (const n of ['undefined', 'NaN', 'Infinity', 'globalThis', 'arguments',
                   'window', 'self', 'globalThis']) names.add(n);
  return names;
}

// ── Inline <script> extraction (index.html boot/config blocks run in the
// same global scope as the bundle, and bundle files may reference globals
// they define — e.g. server-injected config). ──────────────────────────
function extractInlineScripts(htmlPath) {
  const text = fs.readFileSync(htmlPath, 'utf8');
  const out = [];
  const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
  let m, i = 0;
  while ((m = re.exec(text))) {
    const attrs = m[1] || '';
    if (/\bsrc\s*=/.test(attrs)) continue;
    const typeM = attrs.match(/\btype\s*=\s*["']([^"']+)["']/);
    if (typeM && !/^(text\/javascript|application\/javascript|module)?$/.test(typeM[1])) continue;
    const body = m[2];
    if (body && body.trim()) out.push({ name: `${path.basename(htmlPath)}#inline${++i}`, text: body });
  }
  return out;
}

// ── Declaration collection ─────────────────────────────────────────────
function bindPatternNames(name, add) {
  if (!name) return;
  if (ts.isIdentifier(name)) { add(name.text); return; }
  if (name.kind === SK.ObjectBindingPattern || name.kind === SK.ArrayBindingPattern) {
    for (const el of name.elements) {
      if (el.kind === SK.BindingElement) bindPatternNames(el.name, add);
    }
  }
}

function isFunctionLike(node) {
  return ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) ||
         ts.isArrowFunction(node) || ts.isMethodDeclaration(node) ||
         ts.isConstructorDeclaration(node) || ts.isGetAccessorDeclaration(node) ||
         ts.isSetAccessorDeclaration(node);
}

// Collect every declaration belonging to the FUNCTION/GLOBAL scope whose
// body is `node` — i.e. walk without crossing nested function-likes.
// var/let/const/class all land in this set (hoisting approximation, see
// header). Function/class declarations contribute their name but their
// bodies are NOT entered (they are their own scopes).
function collectScopeDeclarations(node, add) {
  function walk(n) {
    if (ts.isVariableDeclaration(n)) bindPatternNames(n.name, add);
    if (ts.isFunctionDeclaration(n)) { if (n.name) add(n.name.text); return; } // boundary
    if (ts.isClassDeclaration(n)) { if (n.name) add(n.name.text); return; }     // boundary
    if (isFunctionLike(n)) return;                                              // boundary
    if (ts.isImportClause(n)) { if (n.name) add(n.name.text); return; }
    if (ts.isNamespaceImport(n)) { if (n.name) add(n.name.text); return; }
    if (ts.isImportSpecifier(n)) { if (n.name) add(n.name.text); return; }
    ts.forEachChild(n, walk);
  }
  walk(node);
}

// window.X / globalThis.X / <alias>.X assignment targets → global declarations.
function collectWindowAssignments(node, add, aliases) {
  function walk(n) {
    if (n.kind === SK.BinaryExpression && n.operatorToken.kind === SK.EqualsToken) {
      const g = globalMemberName(n.left, aliases);
      if (g) add(g);
    }
    if (ts.isCallExpression(n) && ts.isPropertyAccessExpression(n.expression) &&
        ts.isIdentifier(n.expression.expression) &&
        n.expression.expression.text === 'Object' &&
        n.expression.name.text === 'assign' && n.arguments.length >= 2 &&
        isWindowRef(n.arguments[0], aliases)) {
      const source = unwrapExpression(n.arguments[1]);
      if (source && ts.isObjectLiteralExpression(source)) {
        for (const property of source.properties) {
          if (ts.isShorthandPropertyAssignment(property)) {
            add(property.name.text);
          } else if ((ts.isPropertyAssignment(property) ||
                      ts.isMethodDeclaration(property)) && property.name &&
                     (ts.isIdentifier(property.name) ||
                      ts.isStringLiteral(property.name) ||
                      ts.isNumericLiteral(property.name))) {
            add(property.name.text);
          }
        }
      }
    }
    ts.forEachChild(n, walk);
  }
  walk(node);
}

// ── IIFE window-alias detection ─────────────────────────────────────────
// `(function (global) { … })(typeof window !== 'undefined' ? window : this)`
// — the UMD-ish wrapper api.js / voice.js use. The parameter that RECEIVES
// window is an alias: `<alias>.X = …` declares a global, `<alias>.X` reads it.
function isWindowishArg(e) {
  e = unwrapExpression(e);
  if (ts.isIdentifier(e) && (e.text === 'window' || e.text === 'globalThis')) return true;
  if (ts.isCallExpression(e) && ts.isIdentifier(e.expression) &&
      e.expression.text === 'globals' && e.arguments.length === 0) return true;
  if (e.kind === SK.ThisKeyword) return true; // top-level this === window (sloppy script)
  if (ts.isParenthesizedExpression(e)) return isWindowishArg(e.expression);
  if (ts.isConditionalExpression(e)) {
    return isWindowishArg(e.whenTrue) || isWindowishArg(e.whenFalse);
  }
  return false;
}

function findWindowAliases(sf) {
  const aliases = new Set();
  (function walk(n) {
    if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name) &&
        n.initializer && isWindowishArg(n.initializer)) {
      aliases.add(n.name.text);
    }
    if (ts.isCallExpression(n)) {
      let fn = n.expression;
      while (ts.isParenthesizedExpression(fn)) fn = fn.expression;
      if (ts.isFunctionExpression(fn) || ts.isArrowFunction(fn)) {
        const params = fn.parameters || [];
        n.arguments.forEach((arg, i) => {
          if (i >= params.length) return;
          if (!ts.isIdentifier(params[i].name)) return;
          if (isWindowishArg(arg)) aliases.add(params[i].name.text);
        });
      }
    }
    ts.forEachChild(n, walk);
  })(sf);
  return aliases;
}

function collectWindowContractProperties(sf, add) {
  function propertyName(member) {
    const name = member && member.name;
    return name && (ts.isIdentifier(name) || ts.isStringLiteral(name) ||
                    ts.isNumericLiteral(name)) ? name.text : null;
  }
  function typeContainsWindow(type) {
    if (!type) return false;
    if (ts.isTypeReferenceNode(type) && ts.isIdentifier(type.typeName)) {
      return type.typeName.text === 'Window';
    }
    if (ts.isIntersectionTypeNode(type) || ts.isUnionTypeNode(type)) {
      return type.types.some(typeContainsWindow);
    }
    if (ts.isParenthesizedTypeNode(type)) return typeContainsWindow(type.type);
    return false;
  }
  function addTypeLiteralMembers(type) {
    if (ts.isTypeLiteralNode(type)) {
      for (const member of type.members) {
        const name = propertyName(member);
        if (name) add(name);
      }
    } else if (ts.isIntersectionTypeNode(type) || ts.isUnionTypeNode(type)) {
      for (const item of type.types) addTypeLiteralMembers(item);
    } else if (ts.isParenthesizedTypeNode(type)) {
      addTypeLiteralMembers(type.type);
    }
  }
  for (const statement of sf.statements) {
    if (ts.isTypeAliasDeclaration(statement) &&
        typeContainsWindow(statement.type)) {
      addTypeLiteralMembers(statement.type);
    }
    if (ts.isInterfaceDeclaration(statement) && statement.heritageClauses &&
        statement.heritageClauses.some((clause) => clause.types.some(
          (item) => ts.isIdentifier(item.expression) &&
                    item.expression.text === 'Window'))) {
      for (const member of statement.members) {
        const name = propertyName(member);
        if (name) add(name);
      }
    }
  }
}

function unwrapExpression(node) {
  while (node && (ts.isParenthesizedExpression(node) ||
         ts.isAsExpression(node) || ts.isTypeAssertionExpression(node) ||
         ts.isNonNullExpression(node) ||
         (ts.isSatisfiesExpression && ts.isSatisfiesExpression(node)))) {
    node = node.expression;
  }
  return node;
}

function isWindowRef(node, aliases) {
  node = unwrapExpression(node);
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) &&
      node.expression.text === 'globals' && node.arguments.length === 0) {
    return true;
  }
  return ts.isIdentifier(node) &&
         (node.text === 'window' || node.text === 'globalThis' || aliases.has(node.text));
}

function globalMemberName(expr, aliases) {
  // window.X / globalThis.X / <alias>.X  (property or string-literal access)
  if (ts.isPropertyAccessExpression(expr) && isWindowRef(expr.expression, aliases)) {
    return expr.name.text;
  }
  if (ts.isElementAccessExpression(expr) && isWindowRef(expr.expression, aliases) &&
      expr.argumentExpression && ts.isStringLiteral(expr.argumentExpression)) {
    return expr.argumentExpression.text;
  }
  return null;
}

function parseSourceFile(unit) {
  const kind = /\.tsx?$/.test(unit.name) ? ts.ScriptKind.TS : ts.ScriptKind.JS;
  return ts.createSourceFile(
    unit.name, unit.text, ts.ScriptTarget.ES2022, true, kind);
}

// ── typeof-guard extraction ─────────────────────────────────────────────
// `typeof X !== 'undefined'` → X provably exists when the test is TRUE;
// `typeof X === 'undefined'` → X provably exists when the test is FALSE;
// `typeof X === 'function'` (any concrete type) → X exists when TRUE.
function typeofTargetName(expr) {
  if (ts.isIdentifier(expr)) return expr.text;
  if (ts.isPropertyAccessExpression(expr) && ts.isIdentifier(expr.expression) &&
      (expr.expression.text === 'window' || expr.expression.text === 'globalThis')) {
    return expr.name.text;
  }
  return null;
}

function typeofGuards(test) {
  const out = [];
  if (!test) return out;
  if (ts.isParenthesizedExpression(test)) return typeofGuards(test.expression);
  if (ts.isPrefixUnaryExpression(test) && test.operator === SK.ExclamationToken) {
    for (const g of typeofGuards(test.operand)) {
      out.push({ name: g.name, when: g.when === 'then' ? 'else' : 'then' });
    }
    return out;
  }
  if (ts.isBinaryExpression(test)) {
    const op = test.operatorToken.kind;
    const l = test.left, r = test.right;
    const te = ts.isTypeOfExpression(l) ? l : (ts.isTypeOfExpression(r) ? r : null);
    const lit = ts.isStringLiteral(l) ? l.text : (ts.isStringLiteral(r) ? r.text : null);
    if (te && lit != null) {
      const name = typeofTargetName(te.expression);
      if (name) {
        const eq = op === SK.EqualsEqualsToken || op === SK.EqualsEqualsEqualsToken;
        const neq = op === SK.ExclamationEqualsToken || op === SK.ExclamationEqualsEqualsToken;
        if (lit === 'undefined') {
          if (neq) out.push({ name, when: 'then' });
          else if (eq) out.push({ name, when: 'else' });
        } else if (eq) {
          out.push({ name, when: 'then' });
        }
      }
    }
    if (op === SK.AmpersandAmpersandToken || op === SK.BarBarToken) {
      out.push(...typeofGuards(l), ...typeofGuards(r));
    }
  }
  return out;
}

// ── Reference resolution ───────────────────────────────────────────────
function makeScope(parent) {
  return { parent, names: new Set(), guarded: null };
}
function scopeDeclare(scope, name) { scope.names.add(name); }
function scopeResolve(scope, name) {
  for (let s = scope; s; s = s.parent) if (s.names.has(name)) return true;
  return false;
}
function scopeGuarded(scope, name) {
  for (let s = scope; s; s = s.parent) if (s.guarded && s.guarded.has(name)) return true;
  return false;
}
function withGuards(scope, names) {
  if (!names.length) return scope;
  const s = makeScope(scope);
  s.guarded = new Set(names);
  return s;
}

function analyze(opts) {
  const { units, externals, globalDecls, aliasesByFile } = opts;
  const violations = [];
  const sloppy = [];
  const probed = [];
  let refCount = 0;

  function reportGlobalRead(name, node, sf, file, scope) {
    refCount++;
    if (globalDecls.has(name) || externals.has(name)) return;
    if (scope && scopeGuarded(scope, name)) return;
    const { line, character } = sf.getLineAndCharacterOfPosition(node.getStart());
    violations.push({ file, line: line + 1, col: character + 1, name });
  }

  function handleIdentifier(node, scope, sf, file, aliases) {
    const p = node.parent;
    if (!p) return;
    const name = node.text;

    // ── Non-reference positions ──
    if (ts.isPropertyAccessExpression(p) && p.name === node) {
      // window.X read — a PLAIN read of a missing window property yields
      // undefined (safe); only a CHAINED (window.X.deep) or CALLED
      // (window.X()) access on an unresolved X can throw.
      if (isWindowRef(p.expression, aliases)) {
        const gp = p.parent;
        const throwing = gp && (
          (ts.isPropertyAccessExpression(gp) && gp.expression === p) ||
          (ts.isElementAccessExpression(gp) && gp.expression === p) ||
          (ts.isCallExpression(gp) && gp.expression === p));
        if (throwing) reportGlobalRead(name, node, sf, file, scope);
      }
      return;
    }
    if (ts.isPropertyAssignment(p) && p.name === node) return;          // {key: v}
    if (ts.isPropertySignature(p) && p.name === node) return;
    if (ts.isMemberName && false) return;
    if ((ts.isMethodDeclaration(p) || ts.isMethodSignature(p) ||
         ts.isGetAccessorDeclaration(p) || ts.isSetAccessorDeclaration(p) ||
         ts.isPropertyDeclaration(p)) && p.name === node) return;
    if (ts.isBindingElement(p) && p.name === node) return;              // declares
    if (ts.isBindingElement(p) && p.propertyName === node) return;      // {key: local}
    if ((ts.isVariableDeclaration(p) || ts.isParameter(p) ||
         ts.isFunctionDeclaration(p) || ts.isFunctionExpression(p) ||
         ts.isClassDeclaration(p) || ts.isClassExpression(p) ||
         ts.isEnumDeclaration(p) || ts.isEnumMember(p) ||
         ts.isTypeAliasDeclaration(p) || ts.isInterfaceDeclaration(p) ||
         ts.isTypeParameterDeclaration(p) || ts.isImportSpecifier(p) ||
         ts.isImportClause(p) || ts.isNamespaceImport(p) ||
         ts.isModuleDeclaration(p)) && p.name === node) return;         // declares
    if (ts.isLabeledStatement(p) && p.label === node) return;
    if ((ts.isBreakStatement(p) || ts.isContinueStatement(p)) && p.label === node) return;
    if (ts.isExportSpecifier(p)) return;
    if (p.kind === SK.QualifiedName) return;
    if (p.kind === SK.MetaProperty) return;
    // Type positions (rare in .js but cheap to exclude)
    if (ts.isTypeReferenceNode && ts.isTypeReferenceNode(p)) return;

    // ── typeof probe: not a read (only record UNRESOLVED probes — locals
    //    and known globals are noise) ──
    if (ts.isTypeOfExpression(p)) {
      if (!scopeResolve(scope, name) && !globalDecls.has(name) && !externals.has(name)) {
        probed.push({ file, name });
      }
      return;
    }

    // ── Writes ──
    if (p.kind === SK.BinaryExpression && p.left === node) {
      const op = p.operatorToken.kind;
      if (op === SK.EqualsToken) {
        // Plain assignment: write-only. Undeclared → sloppy global decl.
        if (!scopeResolve(scope, name) && !globalDecls.has(name) && !externals.has(name)) {
          const { line, character } = sf.getLineAndCharacterOfPosition(node.getStart());
          sloppy.push({ file, line: line + 1, col: character + 1, name });
          globalDecls.set(name, file); // subsequent reads are legal (sloppy semantics)
        }
        return;
      }
      // Compound assignment (+= …) reads AND writes — fall through to read.
    }

    // ── Read ──
    refCount++;
    if (scopeResolve(scope, name)) return;
    if (globalDecls.has(name) || externals.has(name)) return;
    if (scopeGuarded(scope, name)) return;
    const { line, character } = sf.getLineAndCharacterOfPosition(node.getStart());
    violations.push({ file, line: line + 1, col: character + 1, name });
  }

  function visit(node, scope, sf, file, aliases) {
    if (ts.isIdentifier(node)) {
      handleIdentifier(node, scope, sf, file, aliases);
      return; // identifiers have no children of interest
    }

    // ── typeof-guard control-flow scopes ──
    if (ts.isIfStatement(node)) {
      const guards = typeofGuards(node.expression);
      visit(node.expression, scope, sf, file, aliases);
      visit(node.thenStatement,
            withGuards(scope, guards.filter((g) => g.when === 'then').map((g) => g.name)),
            sf, file, aliases);
      if (node.elseStatement) {
        visit(node.elseStatement,
              withGuards(scope, guards.filter((g) => g.when === 'else').map((g) => g.name)),
              sf, file, aliases);
      }
      return;
    }
    if (ts.isConditionalExpression(node)) {
      const guards = typeofGuards(node.condition);
      visit(node.condition, scope, sf, file, aliases);
      visit(node.whenTrue,
            withGuards(scope, guards.filter((g) => g.when === 'then').map((g) => g.name)),
            sf, file, aliases);
      visit(node.whenFalse,
            withGuards(scope, guards.filter((g) => g.when === 'else').map((g) => g.name)),
            sf, file, aliases);
      return;
    }
    if (ts.isBinaryExpression(node) &&
        (node.operatorToken.kind === SK.AmpersandAmpersandToken ||
         node.operatorToken.kind === SK.BarBarToken)) {
      // `typeof X !== 'undefined' && X.go()` — the right side runs only when
      // the left affirmed X (for &&: 'then' guards; for ||: 'else' guards).
      visit(node.left, scope, sf, file, aliases);
      const want = node.operatorToken.kind === SK.AmpersandAmpersandToken ? 'then' : 'else';
      const guards = typeofGuards(node.left).filter((g) => g.when === want).map((g) => g.name);
      visit(node.right, withGuards(scope, guards), sf, file, aliases);
      return;
    }

    if (isFunctionLike(node)) {
      const fnScope = makeScope(scope);
      scopeDeclare(fnScope, 'arguments');
      // FunctionExpression name binds inside its own body.
      if (ts.isFunctionExpression(node) && node.name) scopeDeclare(fnScope, node.name.text);
      for (const param of (node.parameters || [])) {
        bindPatternNames(param.name, (n) => scopeDeclare(fnScope, n));
        if (param.initializer) visit(param.initializer, fnScope, sf, file, aliases);
      }
      if (node.body) {
        collectScopeDeclarations(node.body, (n) => scopeDeclare(fnScope, n));
        visit(node.body, fnScope, sf, file, aliases);
      }
      // Heritage/type params of arrows don't exist; decorators ignored (JS).
      return;
    }

    if (ts.isClassExpression(node)) {
      const clsScope = makeScope(scope);
      if (node.name) scopeDeclare(clsScope, node.name.text);
      ts.forEachChild(node, (c) => visit(c, clsScope, sf, file, aliases));
      return;
    }

    if (ts.isCatchClause(node)) {
      const cScope = makeScope(scope);
      if (node.variableDeclaration) bindPatternNames(node.variableDeclaration.name, (n) => scopeDeclare(cScope, n));
      ts.forEachChild(node, (c) => visit(c, cScope, sf, file, aliases));
      return;
    }

    ts.forEachChild(node, (c) => visit(c, scope, sf, file, aliases));
  }

  for (const unit of units) {
    const sf = parseSourceFile(unit);
    const globalScope = makeScope(null);
    // Top-level scope IS the shared global namespace — locals of this file
    // resolve via globalDecls; per-file top-level bindings were pre-collected.
    visit(sf, globalScope, sf, unit.name, aliasesByFile.get(unit.name) || new Set());
  }
  return { violations, sloppy, probed, refCount };
}

// ── Driver ─────────────────────────────────────────────────────────────
function main() {
  const t0 = Date.now();
  const spec = JSON.parse(fs.readFileSync(SPEC_PATH, 'utf8'));
  const externals = loadTsLibGlobals();
  for (const g of (spec.extraGlobals || [])) externals.add(g);

  const units = [];
  for (const rel of (spec.files || [])) {
    const abs = path.join(ROOT, rel);
    units.push({ name: rel, text: fs.readFileSync(abs, 'utf8') });
  }
  for (const htmlRel of (spec.html || [])) {
    for (const u of extractInlineScripts(path.join(ROOT, htmlRel))) units.push(u);
  }
  const declarationUnits = [];
  for (const rel of (spec.declarationFiles || [])) {
    const abs = path.join(ROOT, rel);
    declarationUnits.push({ name: rel, text: fs.readFileSync(abs, 'utf8') });
  }

  // Pass 1: global union (top-level decls of every unit + window.X assigns).
  const globalDecls = new Map(); // name -> first file
  const aliasesByFile = new Map();
  for (const unit of units) {
    const sf = parseSourceFile(unit);
    const aliases = findWindowAliases(sf);
    aliasesByFile.set(unit.name, aliases);
    collectScopeDeclarations(sf, (n) => { if (!globalDecls.has(n)) globalDecls.set(n, unit.name); });
    collectWindowAssignments(sf, (n) => { if (!globalDecls.has(n)) globalDecls.set(n, unit.name); }, aliases);
  }
  // Vite owners execute as modules, so only their explicit window exports
  // join the classic shared-global namespace. Imported/module-local names do
  // not satisfy classic reads.
  for (const unit of declarationUnits) {
    const sf = parseSourceFile(unit);
    const aliases = findWindowAliases(sf);
    collectWindowAssignments(
      sf,
      (n) => { if (!globalDecls.has(n)) globalDecls.set(n, unit.name); },
      aliases,
    );
    collectWindowContractProperties(
      sf, (n) => { if (!globalDecls.has(n)) globalDecls.set(n, unit.name); });
  }

  // Pass 2: resolve.
  const { violations, sloppy, probed, refCount } = analyze({ units, externals, globalDecls, aliasesByFile });

  // Dedupe probed (informational; an identifier may be typeof-probed often).
  const probedSeen = new Set();
  const probedUnique = probed.filter((p) => {
    const k = `${p.file}${p.name}`;
    if (probedSeen.has(k)) return false;
    probedSeen.add(k);
    return true;
  });
  // A probed name that is ALSO never declared globally: optional external —
  // keep only those (probes of known globals are noise).
  const probedUnknown = probedUnique.filter((p) => !globalDecls.has(p.name) && !externals.has(p.name));

  console.log(JSON.stringify({
    violations, sloppy, probed: probedUnknown,
    stats: {
      files: units.length,
      globals: globalDecls.size,
      externals: externals.size,
      refs: refCount,
      ms: Date.now() - t0,
    },
  }));
}

main();
