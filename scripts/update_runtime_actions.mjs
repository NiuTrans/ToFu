import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { composeRuntime } from './compose_frontend_runtime.mjs';
import {
  collectTopLevelActionReceivers,
  readRepositoryActionReferences,
} from './runtime_action_analysis.mjs';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const runtimePath = path.join(repositoryRoot, 'frontend/src/runtime/app-runtime.js');
const epiloguePath = path.join(
  repositoryRoot, 'frontend/src/runtime/sections/_epilogue.js',
);
const mode = process.argv[2];
if (!['--write', '--check'].includes(mode)) {
  console.error('Usage: node scripts/update_runtime_actions.mjs --write|--check');
  process.exitCode = 2;
} else {
  await composeRuntime({ check: mode === '--check' });
}
const source = fs.readFileSync(runtimePath, 'utf8');
const epilogueSource = fs.readFileSync(epiloguePath, 'utf8');
const sortedActionNames = collectTopLevelActionReceivers({
  definitionPath: runtimePath,
  definitionSource: source,
  references: readRepositoryActionReferences(repositoryRoot),
  seedNames: ['openTradingMode', '_openActiveCompaction'],
});

const generatedBlock = [
  '// BEGIN GENERATED RUNTIME ACTIONS — scripts/update_runtime_actions.mjs',
  'const runtimeActions = Object.freeze({',
  ...sortedActionNames.map((name) => `  ${name},`),
  '});',
  '// END GENERATED RUNTIME ACTIONS',
].join('\n');

const blockPattern = /\/\/ BEGIN GENERATED RUNTIME ACTIONS[^\n]*\n[\s\S]*?\/\/ END GENERATED RUNTIME ACTIONS/;
const legacyPattern = /const runtimeActions = \{ openTradingMode, _openActiveCompaction \};/;
const pattern = blockPattern.test(epilogueSource) ? blockPattern : legacyPattern;
if (!pattern.test(epilogueSource)) {
  throw new Error(`Could not find the generated action block in ${epiloguePath}`);
}
const nextEpilogue = epilogueSource.replace(pattern, generatedBlock);
if (mode === '--write') {
  if (nextEpilogue !== epilogueSource) fs.writeFileSync(epiloguePath, nextEpilogue);
  await composeRuntime();
  console.log(`Runtime action map contains ${sortedActionNames.length} module-private functions.`);
} else if (mode === '--check') {
  if (nextEpilogue !== epilogueSource) {
    console.error('Runtime action map is stale. Run: npm run generate:actions');
    process.exitCode = 1;
  }
}
