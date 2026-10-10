/**
 * Responsibility: inventory browser HTTP calls without executing browser code.
 * Entry points: scanApiSources(sources), collectApiSources(root), CLI JSON output.
 * Dependencies: TypeScript parser/binder; live server route validation belongs
 * to tests/frontend/test_frontend_api_contract.py, not this source inventory.
 */
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const verbs = { get: 'GET', post: 'POST', put: 'PUT', patch: 'PATCH', del: 'DELETE', stream: 'GET' };
const builders = new Set(['resolvePath', '_resolve', 'apiUrl']);
const unknown = '<dynamic>';

export function scanApiSources(sources) {
  const files = new Map(Object.entries(sources));
  const host = ts.createCompilerHost({ allowJs: true, noLib: true });
  host.getCurrentDirectory = () => '/';
  host.getSourceFile = (name, languageVersion) => files.has(name)
    ? ts.createSourceFile(name, files.get(name), languageVersion, true) : undefined;
  host.fileExists = (name) => files.has(name);
  host.readFile = (name) => files.get(name);
  host.resolveModuleNames = (names, containingFile) => names.map((name) => {
    if (!name.startsWith('.')) return undefined;
    const base = path.posix.normalize(path.posix.join(path.posix.dirname(containingFile), name)).replace(/^\//, '');
    const resolvedFileName = [base, `${base}.ts`, `${base}.js`, `${base}/index.ts`, `${base}/index.js`]
      .find((candidate) => files.has(candidate));
    return resolvedFileName ? { resolvedFileName } : undefined;
  });
  const program = ts.createProgram([...files.keys()], { allowJs: true, noLib: true }, host);
  const checker = program.getTypeChecker();
  const endpoints = [];
  const deferred = [];
  const forwarders = [];
  const errors = [];

  function declaration(node) {
    let symbol = checker.getSymbolAtLocation(node);
    if (symbol?.flags & ts.SymbolFlags.Alias) symbol = checker.getAliasedSymbol(symbol);
    return symbol?.valueDeclaration;
  }

  function bound(node, bindings) {
    const dec = ts.isIdentifier(node) ? declaration(node) : null;
    const value = bindings.get(dec);
    return value && !Array.isArray(value) ? value : null;
  }

  function propertyValue(node, key, bindings, seen = new Set()) {
    if (!node || seen.has(node)) return null;
    seen = new Set(seen).add(node);
    if (ts.isIdentifier(node)) {
      const value = bound(node, bindings);
      if (value && !value.node) return { missing: true };
      return value ? propertyValue(value.node, key, value.bindings, seen)
        : propertyValue(declaration(node)?.initializer, key, bindings, seen);
    }
    if (ts.isObjectLiteralExpression(node)) {
      for (const property of [...node.properties].reverse()) {
        if (property.name?.getText().replace(/['"]/g, '') === key) {
          return { node: property.initializer ?? property.name, bindings };
        }
        if (ts.isSpreadAssignment(property)) {
          const value = propertyValue(property.expression, key, bindings, seen);
          if (value && !value.missing) return value;
        }
      }
      return { missing: true };
    }
    return null;
  }

  // Evaluate route syntax only. Unknown domain values become one URL segment;
  // unknown whole paths are reported separately, never treated as checked.
  function values(node, bindings = new Map(), seen = new Set()) {
    if (!node || seen.has(node)) return [unknown];
    seen = new Set(seen).add(node);
    if (ts.isStringLiteralLike(node) || ts.isNumericLiteral(node)) return [node.text];
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
        || ts.isNonNullExpression(node) || ts.isTypeAssertionExpression(node)) {
      return values(node.expression, bindings, seen);
    }
    if (ts.isIdentifier(node)) {
      const dec = declaration(node);
      if (bindings.has(dec)) {
        const value = bindings.get(dec);
        return Array.isArray(value) ? value : values(value.node, value.bindings, seen);
      }
      return values(dec?.initializer, bindings, seen);
    }
    if (ts.isPropertyAccessExpression(node)) {
      const value = propertyValue(node.expression, node.name.text, bindings);
      if (value?.missing) return [];
      if (value) return values(value.node, value.bindings, seen);
    }
    if (ts.isBinaryExpression(node) && [ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken]
      .includes(node.operatorToken.kind)) {
      const left = values(node.left, bindings, seen);
      const present = node.operatorToken.kind === ts.SyntaxKind.BarBarToken
        ? left.filter(Boolean) : left;
      return present.length ? present : values(node.right, bindings, seen);
    }
    if (ts.isTemplateExpression(node)) {
      let out = [node.head.text];
      for (const span of node.templateSpans) {
        out = product(out, values(span.expression, bindings, seen))
          .map((value) => value + span.literal.text);
      }
      return out;
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      return product(values(node.left, bindings, seen), values(node.right, bindings, seen));
    }
    if (ts.isConditionalExpression(node)) {
      return [...new Set([...values(node.whenTrue, bindings, seen), ...values(node.whenFalse, bindings, seen)])];
    }
    if (ts.isCallExpression(node)) {
      const name = ts.isIdentifier(node.expression) ? node.expression.text : '';
      if (builders.has(name)) return values(node.arguments[0], bindings, seen);
      if (name === 'encodeURIComponent' || name === 'segment') return [unknown];
      const dec = declaration(node.expression);
      const fn = dec?.initializer ?? dec;
      if (fn && (ts.isArrowFunction(fn) || ts.isFunctionExpression(fn) || ts.isFunctionDeclaration(fn))) {
        const next = new Map(bindings);
        fn.parameters.forEach((param, index) => next.set(param, values(node.arguments[index], bindings, seen)));
        if (!ts.isBlock(fn.body)) return values(fn.body, next, seen);
        const returns = fn.body.statements.filter(ts.isReturnStatement);
        if (returns.length === 1) return values(returns[0].expression, next, seen);
      }
    }
    return [unknown];
  }

  function product(left, right) {
    if (left.length * right.length > 64) return [unknown];
    return left.flatMap((a) => right.map((b) => a + b));
  }

  function optionMethod(node, bindings = new Map(), seen = new Set()) {
    if (!node) return null;
    if (seen.has(node)) return [unknown];
    seen = new Set(seen).add(node);
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)) {
      return optionMethod(node.expression, bindings, seen);
    }
    if (ts.isIdentifier(node)) {
      const value = bound(node, bindings);
      return value ? optionMethod(value.node, value.bindings, seen)
        : optionMethod(declaration(node)?.initializer, bindings, seen);
    }
    if (ts.isBinaryExpression(node) && [ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken]
      .includes(node.operatorToken.kind)) {
      return optionMethod(node.left, bindings, seen) ?? optionMethod(node.right, bindings, seen);
    }
    if (ts.isCallExpression(node) && node.expression.getText() === 'Object.assign') {
      let method = null;
      for (const argument of node.arguments) method = optionMethod(argument, bindings, seen) ?? method;
      return method;
    }
    if (!ts.isObjectLiteralExpression(node)) return null;
    let method = null;
    for (const property of node.properties) {
      if (ts.isSpreadAssignment(property)) method = optionMethod(property.expression, bindings, seen) ?? method;
      else if (property.name?.getText().replace(/['"]/g, '') === 'method') {
        method = values(property.initializer, bindings);
      }
    }
    return method?.map((value) => value === unknown ? value : value.toUpperCase()) ?? null;
  }

  const contexts = new Map();
  for (const source of program.getSourceFiles()) {
    const imported = new Map();
    const namespaces = new Set();
    for (const statement of source.statements) {
      if (!ts.isImportDeclaration(statement) || !/\/transport$/.test(statement.moduleSpecifier.text)) continue;
      const names = statement.importClause?.namedBindings;
      if (names && ts.isNamespaceImport(names)) namespaces.add(names.name.text);
      if (names && ts.isNamedImports(names)) {
        for (const entry of names.elements) imported.set(entry.name.text, (entry.propertyName ?? entry.name).text);
      }
    }
    contexts.set(source, { imported, namespaces,
      registry: source.fileName.endsWith('/runtime/sections/api.js') });
  }

  function requestKind(node, seen = new Set()) {
    if (seen.has(node.expression)) return null;
    seen = new Set(seen).add(node.expression);
    const { imported, namespaces, registry } = contexts.get(node.getSourceFile());
    const simple = ts.isIdentifier(node.expression) ? node.expression.text : '';
    const member = ts.isPropertyAccessExpression(node.expression) ? node.expression.name.text : '';
    const owner = member ? node.expression.expression.getText() : '';
    const name = imported.get(simple) ?? simple;
    if (builders.has(name)) return { builder: true };
    const literal = /^[`'"]\/api\//.test(node.arguments[0]?.getText() ?? '');
    if (imported.get(simple) === 'request' || (simple === 'request' && (registry || literal))
        || (member === 'request' && (namespaces.has(owner)
          || /^(?:window\.)?Api$|^apiTransport$|^_transportOwner$/.test(owner) || literal))
        || (registry && Object.hasOwn(verbs, simple))
        || (/^(?:window\.)?Api$/.test(owner) && Object.hasOwn(verbs, member))) {
      return { verb: verbs[simple || member] };
    }
    const initializer = ts.isIdentifier(node.expression) ? declaration(node.expression)?.initializer : null;
    if (initializer && (ts.isIdentifier(initializer) || ts.isPropertyAccessExpression(initializer))) {
      return requestKind({expression: initializer, arguments: node.arguments,
        getSourceFile: () => initializer.getSourceFile()}, seen);
    }
    return null;
  }

  function functionValue(expression, seen = new Set()) {
    if (!expression || seen.has(expression)) return null;
    seen = new Set(seen).add(expression);
    if (ts.isParenthesizedExpression(expression) || ts.isAsExpression(expression)) {
      return functionValue(expression.expression, seen);
    }
    const dec = declaration(expression);
    const fn = dec?.initializer ?? dec;
    if (fn && (ts.isIdentifier(fn) || ts.isPropertyAccessExpression(fn))) {
      return functionValue(fn, seen);
    }
    return fn?.body && (ts.isFunctionDeclaration(fn) || ts.isFunctionExpression(fn)
      || ts.isArrowFunction(fn) || ts.isMethodDeclaration(fn)) ? fn : null;
  }

  const wrappers = new Map();
  function wrapper(fn, seen = new Set()) {
    if (!fn || seen.has(fn)) return false;
    if (wrappers.has(fn)) return wrappers.get(fn);
    seen = new Set(seen).add(fn);
    let forwards = false;
    function inspect(node) {
      if (ts.isCallExpression(node) && node.arguments.length) {
        const kind = requestKind(node);
        if ((kind && !kind.builder) || wrapper(functionValue(node.expression), seen)) {
          const routes = values(node.arguments[0]);
          if (!routes.some((value) => value.startsWith('/api/'))) {
            function parameter(child) {
              if (ts.isIdentifier(child) && fn.parameters.includes(declaration(child))) forwards = true;
              ts.forEachChild(child, parameter);
            }
            parameter(node.arguments[0]);
          }
        }
      }
      ts.forEachChild(node, inspect);
    }
    inspect(fn.body);
    wrappers.set(fn, forwards);
    return forwards;
  }

  function emit(node, kind, bindings, origin = null) {
    const source = node.getSourceFile();
    const location = source.getLineAndCharacterOfPosition(node.getStart());
    origin ??= { file: source.fileName, line: location.line + 1 };
    const routes = values(node.arguments[0], bindings);
    const methods = kind.builder ? [null] : kind.verb ? [kind.verb]
      : optionMethod(node.arguments[1], bindings) ?? ['GET'];
    let checked = false;
    for (const route of routes) {
      if (!route.startsWith('/api/')) continue;
      checked = true;
      for (const method of methods) endpoints.push({ ...origin,
        path: route.split(/[?#]/)[0].replace(/\/$/, ''), method });
    }
    if (!checked && routes.some((value) => value.includes(unknown))) {
      deferred.push({ ...origin, expression: node.arguments[0].getText() });
    }
  }

  function expand(fn, call, bindings, origin, seen = new Set()) {
    if (seen.has(fn)) {
      errors.push({ ...origin, reason: 'Recursive HTTP forwarder cannot be resolved' });
      return;
    }
    seen = new Set(seen).add(fn);
    const next = new Map(bindings);
    fn.parameters.forEach((parameter, index) => next.set(parameter, {
      node: call.arguments[index] ?? parameter.initializer, bindings,
    }));
    function visit(node) {
      if (ts.isCallExpression(node) && node.arguments.length) {
        const target = functionValue(node.expression);
        if (wrapper(target)) expand(target, node, next, origin, seen);
        else {
          const kind = requestKind(node);
          if (kind) emit(node, kind, next, origin);
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(fn.body);
  }

  for (const source of program.getSourceFiles()) {
    if (source.parseDiagnostics.length) {
      errors.push(...source.parseDiagnostics.map((error) => ({
        file: source.fileName, reason: ts.flattenDiagnosticMessageText(error.messageText, ' '),
      })));
      continue;
    }
    function visit(node) {
      if (ts.isCallExpression(node) && node.arguments.length) {
        const target = functionValue(node.expression);
        const kind = requestKind(node);
        const location = source.getLineAndCharacterOfPosition(node.getStart(source));
        const origin = { file: source.fileName, line: location.line + 1 };
        let parent = node.parent;
        while (parent && !ts.isFunctionLike(parent)) parent = parent.parent;
        if ((wrapper(target) || (kind && !kind.builder)) && wrapper(parent)
            && !values(node.arguments[0]).some((value) => value.startsWith('/api/'))) {
          forwarders.push({ ...origin, expression: node.arguments[0].getText(source) });
        } else if (wrapper(target)) expand(target, node, new Map(), origin);
        else if (kind) emit(node, kind, new Map(), origin);
      }
      // Generated catalogues describe routes as data rather than direct calls.
      if (ts.isObjectLiteralExpression(node)) {
        const properties = new Map(node.properties.filter(ts.isPropertyAssignment)
          .map((property) => [property.name.getText(source).replace(/['"]/g, ''), property.initializer]));
        if (properties.has('route') && properties.has('method')) {
          for (const route of values(properties.get('route'))) {
            if (!route.startsWith('/api/')) continue;
            const location = source.getLineAndCharacterOfPosition(node.getStart(source));
            for (const method of values(properties.get('method'))) endpoints.push({
              file: source.fileName, line: location.line + 1, path: route, method,
            });
          }
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
  return { endpoints, deferred, forwarders, errors };
}

export function collectApiSources(root) {
  const names = execFileSync('git', ['ls-files', '--cached', '--others', '--exclude-standard', '-z', '--', 'frontend/src'], { cwd: root, encoding: 'utf8' }).split('\0');
  const sources = Object.fromEntries([...new Set(names)].filter((name) => /\.(?:ts|js)$/.test(name)
    && !/(?:\.test\.|\/testing\/|\.generated\.js$|\/runtime\/app-runtime\.js$|\/api\/transport\.ts$)/.test(name))
    .filter((name) => fs.existsSync(path.join(root, name)))
    .map((name) => [name, fs.readFileSync(path.join(root, name), 'utf8')]));
  return sources;
}

function main() {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  process.stdout.write(JSON.stringify(scanApiSources(collectApiSources(root))) + '\n');
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
