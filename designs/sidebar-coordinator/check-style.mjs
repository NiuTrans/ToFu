import { readFile } from 'node:fs/promises';
import { styleTokenViolations } from '../../scripts/style_token_policy.mjs';
const text = await readFile(new URL('./prototype.css', import.meta.url), 'utf8');
const violations = styleTokenViolations(text, 'designs/sidebar-coordinator/prototype.css', { themeAgnostic: true });
if (violations.length) {
  console.error(violations.join('\n'));
  process.exitCode = 1;
} else console.log('Prototype CSS: authoritative style-token policy passed.');
