// Trusted installed-CLI metadata bootstrap. Credentials arrive on private stdin,
// and are added to the Node argv array only after the OS process has started.
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const { cliPath, args, token } = JSON.parse(readFileSync(0, 'utf8'));
process.argv = [process.execPath, cliPath, ...args, '--token', token];
await import(pathToFileURL(cliPath).href);
