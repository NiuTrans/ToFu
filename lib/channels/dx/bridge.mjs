#!/usr/bin/env node
// lib/channels/dx/bridge.mjs — one 大象 (Daxiang) IM connection per channel account.
//
// Responsibility: transport only. Logs in through the owner's dx-pura-api
// engine, keeps PERSISTENT IM handlers for the whole process lifetime (the
// engine's subscribe_messages only listens during its call window), writes
// JSON-lines to stdout and executes JSON-lines commands from stdin. It holds
// no policy and no durable state; docs/CHANNELS.md C6 owns the contract.
//
// Launch (credentials only in the child environment, never argv):
//   bash <skill>/scripts/tofu/dx-tofu.sh node bridge.mjs --engine <dx-engine dir>
//   ... --command whoami | --command create_group --name <n> --member <uid>   (one-shot)
// stdout lines:
//   {"type":"status","status":"connected","selfUid":"…"}       IM authenticated
//   {"type":"status","status":"disconnected"|"kicked"|"failed","detail":"…"}
//   {"type":"message","source":"im"|"pub","message":{…JSON-safe SDK message…}}
//   {"type":"response","id":"…","ok":true,"result":{…}} | {"type":"response","id":"…","ok":false,"error":{code,message}}
// stdin lines: {"id":"…","op":"whoami"|"send_text"|"send_markdown"|"reply_message"|"history"|"sessions", …}
//   {"id":"…","op":"download","url":"…","token":"…","path":"…","credentialPrefixes":[…]}
//   streams one media original to path; only URLs under credentialPrefixes
//   are fetched (and receive the file headers). Never logs a URL or token.
// Exit codes: 0 stdin closed (stop), 4 login/engine failure,
//             43 kicked off (another login took the session). The launcher
//             itself exits 42 when the shared SSO session needs owner login.
// Channel loss: the SDK reports every loss (heartbeat degrade, authentication
// timeout or failure, kick-off) as ConnStatusChange(LOGIN_STATUS.DISCONNECTED,
// reason). With autoConnect off the bridge reconnects the existing socket,
// retaining the engine's credential cache. Authentication failure recovery and
// deadlines stay with the engine; failed recovery exits for supervisor restart.
import { createWriteStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
import readline from 'node:readline';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';

const EXIT_FAILED = 4;
const EXIT_KICKED = 43;
const KICK_EVENTS = ['KickOff', 'KickOffWithSameDeviceId'];
// WHATWG Fetch: a request whose redirect count reaches 20 is a network error.
const FETCH_REDIRECT_LIMIT = 20;

function argValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

function write(value) {
  process.stdout.write(JSON.stringify(value) + '\n');
}

// JSON-safe copy of SDK objects: Long-like ids become strings, functions and
// cycles are dropped. No depth or size cut: the adapter owns normalization.
function plain(value, seen = new WeakSet()) {
  if (value === null || value === undefined) return value ?? null;
  const kind = typeof value;
  if (kind === 'string' || kind === 'boolean') return value;
  if (kind === 'number') return Number.isFinite(value) ? value : null;
  if (kind === 'bigint') return value.toString();
  if (kind === 'function' || kind === 'symbol') return undefined;
  if (Array.isArray(value)) return value.map((item) => plain(item, seen)).filter((item) => item !== undefined);
  if (kind === 'object') {
    if (seen.has(value)) return undefined;
    seen.add(value);
    // 64-bit message IDs arrive as Long objects ({low, high}) or wrappers
    // exposing value(); both become exact decimal strings.
    if (value.low !== undefined && value.high !== undefined && typeof value.toString === 'function') {
      return value.toString();
    }
    if (typeof value.value === 'function') {
      try {
        const primitive = value.value();
        if (primitive !== null && typeof primitive !== 'object') return String(primitive);
      } catch { /* not a value wrapper: copy its fields below */ }
    }
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      const copied = plain(item, seen);
      if (copied !== undefined) out[key] = copied;
    }
    return out;
  }
  return undefined;
}

const engineDir = argValue('--engine');
if (!engineDir) {
  write({ type: 'status', status: 'failed', detail: 'missing --engine' });
  process.exit(EXIT_FAILED);
}
// Same authentication defaults as the engine's own entry point (main.js).
process.env.MEITUAN_SSO_CDP_ONLY ??= '1';
process.env.MEITUAN_SSO_AUTH_MODE ??= 'env';
const { BrowserlessDxRuntime } = await import(pathToFileURL(join(engineDir, 'handler.js')).href);
const { decodeDxMessageRows } = await import(pathToFileURL(join(engineDir, 'api', 'messages.js')).href);
const runtime = new BrowserlessDxRuntime();
const sent = new Set();
let connectionReady = null;

function target(command) {
  const out = {};
  for (const key of ['uid', 'gid', 'pid']) if (typeof command[key] === 'string' && command[key]) out[key] = command[key];
  return out;
}

async function decodeQuotes(message) {
  // Quote replies (type 20) carry base64 protobuf parts; decode the quoted
  // sender/mid so the adapter can tell replies to the owner.
  const body = message?.message ?? message?.body ?? {};
  const parts = [];
  try {
    const quoted = typeof body.quoted === 'string' ? JSON.parse(body.quoted) : body.quoted;
    if (Array.isArray(quoted)) parts.push(...quoted);
  } catch { /* malformed quoted JSON: no reply facts */ }
  try {
    const last = typeof body.lastReply === 'string' ? JSON.parse(body.lastReply) : body.lastReply;
    if (last && typeof last === 'object') parts.push(last);
  } catch { /* malformed lastReply JSON: no reply facts */ }
  const decoded = [];
  for (const part of parts) {
    if (!part || typeof part.message !== 'string') continue;
    try {
      for (const row of await decodeDxMessageRows({ msgs: [part.message] })) {
        decoded.push({ mid: plain(row.mid ?? row.msgId ?? row.id), from: plain(row.fromUid ?? row.from ?? row.uid) });
      }
    } catch { /* undecodable quote part: no reply facts */ }
  }
  return decoded;
}

function underPrefix(url, prefixes) {
  return prefixes.some((prefix) => url.href.startsWith(prefix));
}

function refused(code, message) {
  return { ok: false, error: { code, message } };
}

async function downloadMedia(command) {
  // Redirects are followed by hand so that the session headers reach only the
  // file hosts: fetch would forward these custom headers to any redirect target.
  const prefixes = (Array.isArray(command.credentialPrefixes) ? command.credentialPrefixes : [])
    .map(String).filter(Boolean);
  let url;
  try {
    url = new URL(String(command.url ?? ''));
  } catch {
    return refused('invalid_url', 'not a URL');
  }
  if (!underPrefix(url, prefixes)) return refused('untrusted_host', url.host);
  const credentials = await runtime.credentialProvider.getCredentials();
  // The Web SDK's own file download headers (http.common.ts HTTPCommon.getFileBlob).
  const fileHeaders = {
    ai: String(credentials.appId ?? ''), dt: '2', u: String(credentials.uid ?? ''),
    uu: String(credentials.deviceId ?? ''), ck: String(credentials.xsid ?? ''), al: String(credentials.alToken ?? ''),
  };
  if (command.token) fileHeaders['X-Xmftk'] = String(command.token);
  const scheme = url.protocol;
  for (let redirects = 0; ; redirects += 1) {
    let response;
    try {
      response = await fetch(url, { headers: underPrefix(url, prefixes) ? fileHeaders : {}, redirect: 'manual' });
    } catch (error) {
      return refused('network_error', String(error?.cause?.code ?? error?.name ?? 'fetch failed'));
    }
    const location = response.headers.get('location');
    if (response.status >= 300 && response.status < 400 && location) {
      await response.body?.cancel();
      if (redirects >= FETCH_REDIRECT_LIMIT) return refused('redirect_limit', String(FETCH_REDIRECT_LIMIT));
      const next = new URL(location, url);
      if (next.protocol !== 'https:' && next.protocol !== scheme) return refused('insecure_redirect', next.protocol);
      url = next;
      continue;
    }
    if (!response.ok || !response.body) {
      await response.body?.cancel();
      return refused(`http_${response.status}`, `HTTP ${response.status}`);
    }
    const path = String(command.path ?? '');
    try {
      await pipeline(Readable.fromWeb(response.body), createWriteStream(path, { flags: 'wx', mode: 0o600 }));
    } catch (error) {
      return refused('transfer_failed', String(error?.code ?? error?.name ?? 'stream error'));
    }
    return { bytes: (await stat(path)).size, contentType: response.headers.get('content-type') ?? '' };
  }
}

const commands = {
  async whoami() { return runtime.handle({ action: 'get_current_uid' }); },
  async download(command) { return downloadMedia(command); },
  async send_text(command) {
    return runtime.handle({ action: 'send_text', ...target(command), text: String(command.text ?? ''), extension: command.extension });
  },
  async send_markdown(command) {
    return runtime.handle({ action: 'send_markdown', ...target(command), text: String(command.text ?? ''), extension: command.extension });
  },
  async reply_message(command) {
    return runtime.handle({
      action: 'reply_message', ...target(command), text: String(command.text ?? ''),
      replyTo: command.replyTo, markdown: Boolean(command.markdown), extension: command.extension,
    });
  },
  async history(command) {
    return runtime.handle({ action: 'get_history_messages', ...target(command), limit: command.limit, cursorMsg: command.cursor || undefined });
  },
  async sessions(command) {
    return runtime.handle({ action: 'get_session_list', all: true, startMs: command.startMs, endMs: command.endMs, limit: command.limit });
  },
  async create_group(command) {
    return runtime.handle({ action: 'create_group', name: String(command.name ?? ''), memberUids: command.memberUids });
  },
  // {op:"send_image"|"send_attachment", gid|uid|pid, filePath, extension}: one local
  // file as one native message; the engine uploads it to 大象's own storage and
  // sends an image or file message over this session (docs/CHANNELS.md C10).
  async send_image(command) {
    return runtime.handle({ action: 'send_image', ...target(command), filePath: String(command.filePath ?? ''), extension: command.extension });
  },
  async send_attachment(command) {
    return runtime.handle({ action: 'send_attachment', ...target(command), filePath: String(command.filePath ?? ''), extension: command.extension });
  },
};

async function run(command) {
  const handler = commands[command.op];
  if (!handler) return { ok: false, error: { code: 'unsupported_command', message: String(command.op) } };
  // New commands wait for the shared recovery. An already-dispatched write
  // keeps its own result; this transport must never replay it.
  await connectionReady;
  const result = plain(await handler(command)) ?? {};
  if (result.msgUuid) sent.add(String(result.msgUuid));
  if (result.ok === false) return { ok: false, error: result.error ?? { code: 'engine_error', message: '' }, result };
  return { ok: true, result };
}

const oneShot = argValue('--command');
if (oneShot) {
  const command = { op: oneShot };
  if (oneShot === 'create_group') {
    command.name = argValue('--name');
    command.memberUids = [argValue('--member')].filter(Boolean);
  }
  write({ type: 'response', id: 'once', ...(await run(command)) });
  process.exit(0);
}

const sdk = runtime.webSdkRuntime;
let login;
try {
  login = await sdk.login();
} catch (error) {
  write({ type: 'status', status: 'failed', detail: error instanceof Error ? error.message : String(error) });
  process.exit(EXIT_FAILED);
}
const selfUid = String(login?.credentials?.uid ?? '');
const constants = sdk.instance?.CONSTANT ?? {};
const events = constants.MESSAGE_EVENT ?? {};
const DISCONNECTED = constants.LOGIN_STATUS?.DISCONNECTED;
if (typeof DISCONNECTED !== 'number' || typeof sdk.instance?.reconnect !== 'function') {
  write({ type: 'status', status: 'failed', detail: 'SDK lacks disconnect status or socket reconnect' });
  process.exit(EXIT_FAILED);
}

function fail(error) {
  write({ type: 'status', status: 'failed', detail: error instanceof Error ? error.message : String(error) });
  process.exit(EXIT_FAILED);
}

function reconnectTransport() {
  // The SDK's public socket operation preserves the credential provider.
  // The engine's sdk.reconnect() would invalidate it on every network loss.
  sdk.instance.reconnect();
}

async function forward(source, payload) {
  const message = plain(payload) ?? {};
  const uuid = String(message.msgUuid ?? message.uuid ?? '');
  const record = { type: 'message', source, message, ownConnection: Boolean(uuid && sent.has(uuid)) };
  const type = Number(message.type ?? message.msgType ?? 0);
  if (type === 20) record.quotes = await decodeQuotes(message);
  write(record);
}

// Persistent handlers: attached once, for the process lifetime (re-login
// reuses the same SDK instance, so these survive reconnects inside the SDK).
sdk.addEventHandler(String(events.IM_MESSAGE || 'IM_message'), (payload) => { void forward('im', payload); });
sdk.addEventHandler(String(events.PUB_MESSAGE || 'PUB_MESSAGE'), (payload) => { void forward('pub', payload); });
for (const event of KICK_EVENTS) {
  sdk.addEventHandler(event, () => {
    write({ type: 'status', status: 'kicked', detail: event });
    process.exit(EXIT_KICKED);
  });
}
sdk.addEventHandler('ConnStatusChange', (payload) => {
  // The engine fans the SDK's (status, reason) out as one [status, reason] array.
  const [status, reason] = Array.isArray(payload) ? payload : [payload];
  if (Number(status) !== DISCONNECTED || connectionReady) return;
  // A kick-off reports DISCONNECTED before emitting its kick event within the
  // same synchronous SDK call; deferring lets that event terminate the session.
  connectionReady = new Promise((resolve) => setImmediate(resolve)).then(async () => {
    write({ type: 'status', status: 'disconnected', detail: `status ${status} ${reason ?? ''}`.trim() });
    reconnectTransport();
    const recovered = await sdk.login();
    if (String(recovered?.credentials?.uid ?? '') !== selfUid) throw new Error('SDK reconnect changed account identity');
    write({ type: 'status', status: 'connected', selfUid });
  }).catch(fail).finally(() => { connectionReady = null; });
});
sdk.addEventHandler('AuthFail', () => {
  // login() owns the credential refresh and retry. With autoConnect disabled,
  // its next authentication also needs a connected socket. A terminal failure
  // rejects login() and exits before this deferred transport operation runs.
  if (connectionReady) setImmediate(() => {
    if (!connectionReady) return;
    try { reconnectTransport(); } catch (error) { fail(error); }
  });
});
write({ type: 'status', status: 'connected', selfUid });

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on('line', (line) => {
  if (!line.trim()) return;
  let command;
  try {
    command = JSON.parse(line);
  } catch {
    write({ type: 'response', id: null, ok: false, error: { code: 'invalid_json', message: 'command is not JSON' } });
    return;
  }
  run(command).then(
    (outcome) => write({ type: 'response', id: command.id ?? null, ...outcome }),
    (error) => write({ type: 'response', id: command.id ?? null, ok: false,
                       error: { code: 'bridge_error', message: error instanceof Error ? error.message : String(error) } }),
  );
});
// The parent owns the lifetime: a closed stdin (stop or parent death) ends us.
input.on('close', () => process.exit(0));
