/**
 * Shared static analysis for retained-runtime action receivers.
 *
 * Declarative controls can be authored in retained JavaScript, typed feature
 * owners, or HTML fragments.  A receiver is public only when one of those
 * controls names a top-level function owned by the runtime being generated.
 * Keeping that intersection here gives the eager runtime and every lazy
 * runtime one discoverable, identical publication rule.
 */

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const ts = require('typescript');

function filesIn(directory, predicate) {
  if (!fs.existsSync(directory)) return [];
  const paths = [];
  const visit = (currentDirectory) => {
    for (const entry of fs.readdirSync(currentDirectory, { withFileTypes: true })) {
      const entryPath = path.join(currentDirectory, entry.name);
      if (entry.isDirectory()) visit(entryPath);
      else if (entry.isFile() && predicate(entryPath)) paths.push(entryPath);
    }
  };
  visit(directory);
  return paths.sort();
}

function sourceFile(sourcePath) {
  const source = fs.readFileSync(sourcePath, 'utf8');
  const scriptKind = sourcePath.endsWith('.tsx')
    ? ts.ScriptKind.TSX
    : sourcePath.endsWith('.ts')
      ? ts.ScriptKind.TS
      : ts.ScriptKind.JS;
  return ts.createSourceFile(
    sourcePath, source, ts.ScriptTarget.Latest, true, scriptKind,
  );
}

function htmlSourcesIn(directory) {
  return filesIn(directory, (sourcePath) => sourcePath.endsWith('.html'))
    .map((sourcePath) => fs.readFileSync(sourcePath, 'utf8'));
}

export function authoredLiteralFragments(root) {
  const fragments = [];
  const visit = (node) => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      fragments.push(node.text);
      return;
    }
    if (ts.isTemplateExpression(node)) {
      fragments.push(node.head.text);
      for (const span of node.templateSpans) {
        visit(span.expression);
        fragments.push(span.literal.text);
      }
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(root);
  return fragments;
}

export function actionAttributeValues(input) {
  const values = [];
  const marker = /data-tofu-action(?:-[a-z]+)?\s*=\s*/g;
  for (const match of input.matchAll(marker)) {
    const start = (match.index || 0) + match[0].length;
    const quote = input[start];
    if (quote === '"' || quote === "'") {
      const end = input.indexOf(quote, start + 1);
      // Interpolation or concatenation may split an authored attribute. The
      // receiver is in its first fragment, so an unterminated prefix is useful.
      values.push(input.slice(start + 1, end < 0 ? undefined : end));
      continue;
    }
    const end = input.slice(start).search(/[\s>]/);
    values.push(input.slice(start, end < 0 ? undefined : start + end));
  }
  return values;
}

export function expressionLiteralFragments(node) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    return [node.text];
  }
  if (ts.isTemplateExpression(node)) {
    return [node.head.text, ...node.templateSpans.map((span) => span.literal.text)];
  }
  if (ts.isParenthesizedExpression(node)) {
    return expressionLiteralFragments(node.expression);
  }
  if (ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    return [
      ...expressionLiteralFragments(node.left),
      ...expressionLiteralFragments(node.right),
    ];
  }
  if (ts.isConditionalExpression(node)) {
    return [
      ...expressionLiteralFragments(node.whenTrue),
      ...expressionLiteralFragments(node.whenFalse),
    ];
  }
  return [];
}

export function authoredDomActionValues(root) {
  const values = [];
  const actionAttributeName = /^data-tofu-action(?:-[a-z]+)?$/;
  const visit = (node) => {
    if (ts.isCallExpression(node)
        && ts.isPropertyAccessExpression(node.expression)
        && node.expression.name.text === 'setAttribute'
        && node.arguments.length >= 2) {
      const name = node.arguments[0];
      if ((ts.isStringLiteral(name) || ts.isNoSubstitutionTemplateLiteral(name))
          && actionAttributeName.test(name.text)) {
        values.push(...expressionLiteralFragments(node.arguments[1]));
      }
    } else if (ts.isBinaryExpression(node)
        && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && /\.dataset(?:\.|\[['"])(?:tofuAction)[A-Za-z]*/.test(
          node.left.getText(root),
        )) {
      values.push(...expressionLiteralFragments(node.right));
    }
    ts.forEachChild(node, visit);
  };
  visit(root);
  return values;
}

/** Read every authored domain that may declare a `data-tofu-action` value. */
export function readRepositoryActionReferences(repositoryRoot) {
  const retainedSourceFiles = filesIn(
    path.join(repositoryRoot, 'frontend/src/runtime/sections'),
    (sourcePath) => sourcePath.endsWith('.js'),
  ).map(sourceFile);
  const typedSourceFiles = filesIn(
    path.join(repositoryRoot, 'frontend/src'),
    (sourcePath) => sourcePath.endsWith('.ts') || sourcePath.endsWith('.tsx'),
  ).flatMap((sourcePath) => {
    const source = fs.readFileSync(sourcePath, 'utf8');
    return source.includes('data-tofu-action') ? [sourceFile(sourcePath)] : [];
  });
  const rawSources = [
    fs.readFileSync(path.join(repositoryRoot, 'index.html'), 'utf8'),
    ...htmlSourcesIn(path.join(repositoryRoot, 'static/settings_panels')),
    ...htmlSourcesIn(path.join(repositoryRoot, 'frontend/src/application-shell/fragments')),
  ];
  return {
    authoredSourceFiles: [...retainedSourceFiles, ...typedSourceFiles],
    rawSources,
  };
}

/**
 * Return top-level functions owned by `definitionSource` and named by an
 * authored action attribute anywhere in the repository.
 */
export function collectTopLevelActionReceivers({
  definitionPath,
  definitionSource,
  references,
  seedNames = [],
}) {
  const definitions = ts.createSourceFile(
    definitionPath, definitionSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS,
  );
  const topLevelFunctions = new Set(definitions.statements
    .filter((statement) => ts.isFunctionDeclaration(statement) && statement.name)
    .map((statement) => statement.name.text));
  const actionNames = collectAuthoredActionNames({ references, seedNames });
  return [...actionNames]
    .filter((name) => topLevelFunctions.has(name))
    .sort((left, right) => left.localeCompare(right));
}

/** Return every bare receiver named by an authored action attribute. */
export function collectAuthoredActionNames({ references, seedNames = [] }) {
  const actionNames = new Set(seedNames);
  const actionInputs = [
    ...references.authoredSourceFiles.flatMap(authoredLiteralFragments),
    ...references.rawSources,
  ];
  const actionValues = [
    ...actionInputs.flatMap((input) => actionAttributeValues(input)),
    ...references.authoredSourceFiles.flatMap(authoredDomActionValues),
  ];
  for (const attributeValue of actionValues) {
    for (const call of attributeValue.matchAll(/\b([A-Za-z_$][\w$]*)\s*\(/g)) {
      const previous = call.index > 0 ? attributeValue[call.index - 1] : '';
      if (previous === '.') continue;
      actionNames.add(call[1]);
    }
    const bare = /^([A-Za-z_$][\w$]*)$/.exec(attributeValue.trim());
    if (bare) actionNames.add(bare[1]);
  }
  return actionNames;
}
