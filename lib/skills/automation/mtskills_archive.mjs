// Read an explicitly selected source archive; never extract or install it.
// Python supplies the policy-selected HTTPS URL and credential via private stdin.
import { readFileSync } from 'node:fs';
import { once } from 'node:events';

const input = JSON.parse(readFileSync(0, 'utf8'));
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), input.timeoutMilliseconds);
let failureCode = input.exitCodes.httpFailure;
try {
  const response = await fetch(input.url, {
    redirect: 'error', signal: controller.signal,
    headers: { 'access-token': input.token, 'X-MTSKILLS-SSO-Env': input.ssoEnvironment },
  });
  if (response.status === 401 || response.status === 403) {
    failureCode = input.exitCodes.authenticationRequired;
    throw new Error('Owned source authentication required');
  }
  if (!response.ok || !response.body) throw new Error('Archive request failed');
  const declared = response.headers.get('content-length');
  if (declared && Number(declared) > input.maximumBytes) {
    failureCode = input.exitCodes.tooLarge;
    throw new Error('Archive exceeds its bound');
  }
  const reader = response.body.getReader();
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > input.maximumBytes) {
      failureCode = input.exitCodes.tooLarge;
      throw new Error('Archive exceeds its bound');
    }
    if (!process.stdout.write(value)) await once(process.stdout, 'drain');
  }
} catch {
  // No provider body, URL error or credential reaches stderr/stdout. Partial
  // binary stdout is rejected by the parent whenever the process exits nonzero.
  process.exitCode = controller.signal.aborted ? input.exitCodes.timedOut : failureCode;
} finally {
  clearTimeout(timer);
  controller.abort();
}
