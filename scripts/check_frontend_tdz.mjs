#!/usr/bin/env node

/**
 * Reject import-time references to later top-level lexical declarations.
 *
 * Responsibility: statically guard composed retained runtimes against the
 * temporal-dead-zone failure class that syntax checks and TypeScript builds do
 * not reject. Function bodies are deferred; direct module initialization and
 * immediately-invoked function bodies are evaluated in source order.
 */

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import ts from 'typescript';

const root = path.resolve(import.meta.dirname, '..');

function bindingNames(name, output = []) {
  if (ts.isIdentifier(name)) output.push(name.text);
  else for (const element of name.elements || []) {
    if (!ts.isOmittedExpression(element)) bindingNames(element.name, output);
  }
  return output;
}

function lexicalDeclarations(sourceFile) {
  const declarations = new Map();
  for (const statement of sourceFile.statements) {
    if (ts.isVariableStatement(statement)) {
      const flags = statement.declarationList.flags;
      if (!(flags & (ts.NodeFlags.Const | ts.NodeFlags.Let))) continue;
      for (const declaration of statement.declarationList.declarations) {
        for (const name of bindingNames(declaration.name)) {
          if (!declarations.has(name)) declarations.set(name, statement.getStart(sourceFile));
        }
      }
    } else if (ts.isClassDeclaration(statement) && statement.name) {
      declarations.set(statement.name.text, statement.getStart(sourceFile));
    }
  }
  return declarations;
}

function isReference(identifier) {
  const parent = identifier.parent;
  if (!parent) return false;
  if ((ts.isPropertyAccessExpression(parent) && parent.name === identifier)
      || (ts.isPropertyAssignment(parent) && parent.name === identifier)
      || (ts.isMethodDeclaration(parent) && parent.name === identifier)
      || (ts.isPropertyDeclaration(parent) && parent.name === identifier)
      || (ts.isBindingElement(parent) && parent.name === identifier)
      || (ts.isVariableDeclaration(parent) && parent.name === identifier)
      || (ts.isFunctionDeclaration(parent) && parent.name === identifier)
      || (ts.isClassDeclaration(parent) && parent.name === identifier)
      || ts.isImportClause(parent) || ts.isImportSpecifier(parent)
      || ts.isExportSpecifier(parent) || ts.isLabeledStatement(parent)
      || ts.isBreakStatement(parent) || ts.isContinueStatement(parent)) return false;
  return true;
}

function unwrappedFunction(node) {
  let current = node;
  while (ts.isParenthesizedExpression(current)) current = current.expression;
  return ts.isArrowFunction(current) || ts.isFunctionExpression(current) ? current : null;
}

function functionBindings(fn) {
  const names = new Set();
  for (const parameter of fn.parameters) {
    for (const name of bindingNames(parameter.name)) names.add(name);
  }
  function collect(node) {
    if (node !== fn && (ts.isFunctionDeclaration(node)
        || ts.isFunctionExpression(node) || ts.isArrowFunction(node))) {
      if (ts.isFunctionDeclaration(node) && node.name) names.add(node.name.text);
      return;
    }
    if (ts.isVariableDeclaration(node)) {
      for (const name of bindingNames(node.name)) names.add(name);
    } else if (ts.isClassDeclaration(node) && node.name) {
      names.add(node.name.text);
    }
    ts.forEachChild(node, collect);
  }
  collect(fn.body);
  return names;
}

export function findImportTimeTdz(source, filename = 'runtime.js') {
  const sourceFile = ts.createSourceFile(
    filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS,
  );
  const declarations = lexicalDeclarations(sourceFile);
  const violations = [];

  function visit(node, eager = true, shadows = new Set()) {
    if (!node || !eager || ts.isTypeNode(node) || ts.isImportDeclaration(node)) return;
    if (ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node)
        || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node)) return;
    if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) return;
    if (ts.isIdentifier(node) && isReference(node) && !shadows.has(node.text)) {
      const declaration = declarations.get(node.text);
      if (declaration !== undefined && node.getStart(sourceFile) < declaration) {
        const use = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
        const defined = sourceFile.getLineAndCharacterOfPosition(declaration);
        violations.push({
          name: node.text,
          line: use.line + 1,
          column: use.character + 1,
          declarationLine: defined.line + 1,
        });
      }
      return;
    }
    if (ts.isCallExpression(node)) {
      const immediate = unwrappedFunction(node.expression);
      if (immediate) {
        const immediateShadows = new Set([...shadows, ...functionBindings(immediate)]);
        for (const argument of immediate.parameters) {
          visit(argument.initializer, true, immediateShadows);
        }
        visit(immediate.body, true, immediateShadows);
        for (const argument of node.arguments) visit(argument, true, shadows);
        return;
      }
    }
    ts.forEachChild(node, (child) => visit(child, true, shadows));
  }

  for (const statement of sourceFile.statements) visit(statement);
  return violations;
}

export function defaultRuntimePaths() {
  const manifestPath = path.join(root, 'frontend/src/runtime/sections/manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  return [manifest.output, ...manifest.lazyBundles.map((bundle) => bundle.output)];
}

export function checkFiles(paths) {
  const failures = [];
  for (const value of paths) {
    const absolute = path.resolve(root, value);
    const relative = path.relative(root, absolute).split(path.sep).join('/');
    const source = fs.readFileSync(absolute, 'utf8');
    for (const violation of findImportTimeTdz(source, relative)) {
      failures.push(`${relative}:${violation.line}:${violation.column}: `
        + `${violation.name} executes before its lexical declaration on line `
        + `${violation.declarationLine}`);
    }
  }
  if (failures.length) {
    throw new Error(`frontend import-time TDZ gate failed:\n${failures.join('\n')}`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(import.meta.filename)) {
  const paths = process.argv.slice(2);
  try {
    checkFiles(paths.length ? paths : defaultRuntimePaths());
    process.stdout.write('frontend import-time TDZ gate: OK\n');
  } catch (error) {
    process.stderr.write(`${error.message || error}\n`);
    process.exitCode = 1;
  }
}
