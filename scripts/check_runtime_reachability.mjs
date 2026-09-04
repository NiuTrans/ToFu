#!/usr/bin/env node

/**
 * Reject unreachable top-level callables in generated retained runtimes.
 *
 * The retained runtime is one ordered lexical module, so an unreferenced
 * function (or a closed chain of functions) is dead authoring debt. A callable
 * becomes live when module initialization/export code references it, then
 * liveness propagates through its direct callable dependencies. Property names
 * are not references; shorthand values and explicit right-hand sides are.
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import ts from 'typescript';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const manifestPath = path.join(
  repositoryRoot, 'frontend/src/runtime/sections/manifest.json',
);

function isExported(node) {
  return Boolean(node.modifiers?.some(
    (modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword,
  ));
}

function isCallableInitializer(node) {
  return ts.isArrowFunction(node) || ts.isFunctionExpression(node);
}

function isPropertyName(identifier) {
  const parent = identifier.parent;
  return (ts.isPropertyAccessExpression(parent) && parent.name === identifier)
    || (ts.isPropertyAssignment(parent) && parent.name === identifier)
    || (ts.isMethodDeclaration(parent) && parent.name === identifier)
    || (ts.isGetAccessorDeclaration(parent) && parent.name === identifier)
    || (ts.isSetAccessorDeclaration(parent) && parent.name === identifier);
}

/**
 * Return top-level callable bindings that cannot be reached from module
 * initialization or an export.
 */
export function analyzeRuntimeReachability(source, fileName = '<runtime>') {
  const sourceFile = ts.createSourceFile(
    fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS,
  );
  if (sourceFile.parseDiagnostics.length) {
    const diagnostic = sourceFile.parseDiagnostics[0];
    const message = ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n');
    throw new Error(`${fileName} cannot be parsed for reachability: ${message}`);
  }

  const bindings = new Map();
  const ownerByNode = new Map();
  const exported = new Set();

  function register(name, callableNode, nameNode, exportOwner) {
    if (bindings.has(name)) {
      throw new Error(`${fileName} has duplicate top-level callable: ${name}`);
    }
    bindings.set(name, { callableNode, nameNode });
    ownerByNode.set(callableNode, name);
    if (isExported(exportOwner)) exported.add(name);
  }

  for (const statement of sourceFile.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name) {
      register(statement.name.text, statement, statement.name, statement);
      continue;
    }
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name)
          || !declaration.initializer
          || !isCallableInitializer(declaration.initializer)) continue;
      register(
        declaration.name.text,
        declaration.initializer,
        declaration.name,
        statement,
      );
    }
  }

  const dependencies = new Map(
    [...bindings.keys()].map((name) => [name, new Set()]),
  );
  const roots = new Set(exported);

  function enclosingOwner(node) {
    let parent = node.parent;
    while (parent && parent !== sourceFile) {
      const owner = ownerByNode.get(parent);
      if (owner) return owner;
      parent = parent.parent;
    }
    return null;
  }

  function visit(node) {
    if (ts.isIdentifier(node) && bindings.has(node.text)) {
      const binding = bindings.get(node.text);
      if (binding.nameNode !== node && !isPropertyName(node)) {
        const owner = enclosingOwner(node);
        if (owner) dependencies.get(owner).add(node.text);
        else roots.add(node.text);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);

  const reachable = new Set(roots);
  const pending = [...roots];
  while (pending.length) {
    const owner = pending.pop();
    for (const dependency of dependencies.get(owner) || []) {
      if (reachable.has(dependency)) continue;
      reachable.add(dependency);
      pending.push(dependency);
    }
  }

  const unreachable = [];
  for (const [name, binding] of bindings) {
    if (reachable.has(name)) continue;
    const location = sourceFile.getLineAndCharacterOfPosition(
      binding.nameNode.getStart(sourceFile),
    );
    unreachable.push({ name, line: location.line + 1 });
  }
  return { callableCount: bindings.size, unreachable };
}

async function generatedRuntimePaths() {
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  return [
    manifest.output,
    ...(manifest.lazyBundles || []).map((bundle) => bundle.output),
  ].map((relativePath) => path.resolve(repositoryRoot, relativePath));
}

async function main() {
  let callableCount = 0;
  const failures = [];
  const outputs = await generatedRuntimePaths();
  for (const output of outputs) {
    const source = await fs.readFile(output, 'utf8');
    const relativePath = path.relative(repositoryRoot, output).split(path.sep).join('/');
    const result = analyzeRuntimeReachability(source, relativePath);
    callableCount += result.callableCount;
    for (const item of result.unreachable) {
      failures.push(`${relativePath}:${item.line}: ${item.name}`);
    }
  }
  if (failures.length) {
    throw new Error(
      `unreachable retained runtime callables:\n${failures.map((row) => `  ${row}`).join('\n')}`,
    );
  }
  process.stdout.write(
    `runtime-reachability: OK (${callableCount} top-level callables, ${outputs.length} runtimes)\n`,
  );
}

const invokedPath = process.argv[1]
  ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (import.meta.url === invokedPath) await main();
