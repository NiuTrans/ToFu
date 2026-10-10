/**
 * Enforce the declared conversation renderer and projection dependency boundaries.
 * Entry: CLI for check:frontend; checkRendererDependencies for fault fixtures.
 * Dependencies: the ownership contract, TypeScript syntax and filesystem reads.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, extname, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

export function checkRendererDependencies(projectRoot, owners, scope = 'Renderer', supportOwners = []) {
  const modulesRoot = resolve(projectRoot, owners.conversationRendererModules);
  const composition = resolve(projectRoot, owners.conversationRendererComposition);
  const paths = [];
  const visitDirectory = directory => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name);
      if (entry.isDirectory()) visitDirectory(path);
      else if (/\.tsx?$/.test(entry.name) && !/\.(?:test|d)\.tsx?$/.test(entry.name)) paths.push(path);
    }
  };
  visitDirectory(modulesRoot);
  const hasOwnedModules = paths.length > 0;
  // Narrow external collaborators can refer back to projection types. Include
  // those declared owners so a type-only facade cycle cannot hide outside the directory.
  for (const owner of supportOwners) paths.push(resolve(projectRoot, owner));
  const graph = new Map(paths.sort().map(path => [path, []]));
  const failures = [];
  const label = path => relative(projectRoot, path).split(sep).join('/');
  if (!hasOwnedModules) failures.push(`Declared ${scope.toLowerCase()} owner contains no source modules.`);
  for (const path of paths) {
    const source = ts.createSourceFile(path, readFileSync(path, 'utf8'), ts.ScriptTarget.Latest, true);
    const inspect = node => {
      const specifier = ts.isImportDeclaration(node) || ts.isExportDeclaration(node)
        ? node.moduleSpecifier
        : ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword
          ? node.arguments[0] : null;
      if (specifier && ts.isStringLiteral(specifier) && specifier.text.startsWith('.')) {
        let dependency = resolve(dirname(path), specifier.text);
        if (!extname(dependency) || extname(dependency) === '.generated') {
          dependency = [dependency + '.ts', dependency + '.tsx', resolve(dependency, 'index.ts')]
            .find(candidate => graph.has(candidate) || candidate === composition) ?? dependency + '.ts';
        }
        else if (extname(dependency) === '.js') dependency = dependency.slice(0, -3) + '.ts';
        if (dependency === composition) {
          failures.push(`${label(path)} imports its composition facade; use a narrow content or type owner.`);
        }
        if (graph.has(dependency)) graph.get(path).push(dependency);
      }
      ts.forEachChild(node, inspect);
    };
    inspect(source);
  }
  const visited = new Set();
  const active = [];
  const walk = path => {
    const cycleStart = active.indexOf(path);
    if (cycleStart >= 0) {
      failures.push(`${scope} dependency cycle: ` + [...active.slice(cycleStart), path].map(label).join(' -> '));
      return;
    }
    if (visited.has(path)) return;
    active.push(path);
    for (const dependency of graph.get(path)) walk(dependency);
    active.pop();
    visited.add(path);
  };
  for (const path of paths) walk(path);
  return failures;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  const contract = JSON.parse(readFileSync(resolve(projectRoot, 'contracts/frontend_conversation_architecture_v1.json'), 'utf8'));
  const failures = [
    ...checkRendererDependencies(projectRoot, contract.owners),
    ...checkRendererDependencies(projectRoot, {
      conversationRendererModules: contract.owners.viewModelModules,
      conversationRendererComposition: contract.owners.viewModelProjection,
    }, 'View model', [contract.owners.activityAnchorProjection, contract.owners.roundOutlineProjection]),
    ...checkRendererDependencies(projectRoot, {
      conversationRendererModules: contract.owners.markdownRendererModules,
      conversationRendererComposition: contract.owners.markdownRendererComposition,
    }, 'Markdown'),
    ...checkRendererDependencies(projectRoot, {
      conversationRendererModules: contract.owners.checklistCardModules,
      conversationRendererComposition: contract.owners.checklistCardComposition,
    }, 'Checklist card'),
    ...checkRendererDependencies(projectRoot, {
      conversationRendererModules: contract.owners.timerCardModules,
      conversationRendererComposition: contract.owners.timerCardComposition,
    }, 'Timer card'),
    ...checkRendererDependencies(projectRoot, {
      conversationRendererModules: contract.owners.projectReceiptModules,
      conversationRendererComposition: contract.owners.projectReceiptComposition,
    }, 'Project receipt'),
    ...checkRendererDependencies(projectRoot, {
      conversationRendererModules: contract.owners.toolGroupModules,
      conversationRendererComposition: contract.owners.toolGroupComposition,
    }, 'Tool-group UI'),
    ...checkRendererDependencies(projectRoot, {
      conversationRendererModules: contract.owners.motionCardModules,
      conversationRendererComposition: contract.owners.motionCardComposition,
    }, 'Multimedia receipt'),
  ];
  if (failures.length) {
    for (const failure of failures) console.error(failure);
    process.exitCode = 1;
  } else console.log('Conversation, view-model, Markdown and rich-card dependency boundaries: OK');
}
