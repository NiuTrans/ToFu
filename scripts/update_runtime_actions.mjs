import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const runtimePath = path.join(repositoryRoot, 'frontend/src/runtime/app-runtime.js');
const source = fs.readFileSync(runtimePath, 'utf8');
const indexSource = fs.readFileSync(path.join(repositoryRoot, 'index.html'), 'utf8');
const settingsPanelDirectory = path.join(repositoryRoot, 'static/settings_panels');
const settingsPanelSources = fs.readdirSync(settingsPanelDirectory)
  .filter((name) => name.endsWith('.html'))
  .sort()
  .map((name) => fs.readFileSync(path.join(settingsPanelDirectory, name), 'utf8'));
const sourceFile = ts.createSourceFile(
  runtimePath,
  source,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.JS,
);

const topLevelFunctions = new Set(sourceFile.statements
  .filter((statement) => ts.isFunctionDeclaration(statement) && statement.name)
  .map((statement) => statement.name.text));

// Only functions named by declarative event attributes need to cross from the
// module's lexical scope into the private action resolver. A short source
// window reaches the end of each generated tag (or setAttribute call) while
// avoiding the old file-scope behaviour of exporting every declaration.
const actionNames = new Set(['openTradingMode', '_openActiveCompaction']);
for (const input of [source, indexSource, ...settingsPanelSources]) {
  for (const match of input.matchAll(/data-tofu-action/g)) {
    const tail = input.slice(match.index, match.index + 1200);
    const tagEnd = tail.indexOf('>');
    const callEnd = tail.indexOf(');');
    const end = tagEnd >= 0 ? tagEnd : callEnd >= 0 ? callEnd : 240;
    const attribute = tail.slice(0, end);
    for (const call of attribute.matchAll(/\b([A-Za-z_$][\w$]*)\s*\(/g)) {
      if (topLevelFunctions.has(call[1])) actionNames.add(call[1]);
    }
    const bare = /^data-tofu-action(?:-[a-z]+)?\s*=\s*["']([A-Za-z_$][\w$]*)["']/.exec(attribute);
    if (bare && topLevelFunctions.has(bare[1])) actionNames.add(bare[1]);
  }
}
const sortedActionNames = [...actionNames]
  .filter((name) => topLevelFunctions.has(name))
  .sort((left, right) => left.localeCompare(right));

const generatedBlock = [
  '// BEGIN GENERATED RUNTIME ACTIONS — scripts/update_runtime_actions.mjs',
  'const runtimeActions = Object.freeze({',
  ...sortedActionNames.map((name) => `  ${name},`),
  '});',
  '// END GENERATED RUNTIME ACTIONS',
].join('\n');

const blockPattern = /\/\/ BEGIN GENERATED RUNTIME ACTIONS[^\n]*\n[\s\S]*?\/\/ END GENERATED RUNTIME ACTIONS/;
const legacyPattern = /const runtimeActions = \{ openTradingMode, _openActiveCompaction \};/;
const pattern = blockPattern.test(source) ? blockPattern : legacyPattern;
if (!pattern.test(source)) {
  throw new Error(`Could not find the generated action block in ${runtimePath}`);
}
const nextSource = source.replace(pattern, generatedBlock);
const mode = process.argv[2];
if (mode === '--write') {
  if (nextSource !== source) fs.writeFileSync(runtimePath, nextSource);
  console.log(`Runtime action map contains ${sortedActionNames.length} module-private functions.`);
} else if (mode === '--check') {
  if (nextSource !== source) {
    console.error('Runtime action map is stale. Run: npm run generate:actions');
    process.exitCode = 1;
  }
} else {
  console.error('Usage: node scripts/update_runtime_actions.mjs --write|--check');
  process.exitCode = 2;
}
