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
// Exit codes: 0 stdin closed (stop), 3 disconnected, 4 login/engine failure,
//             43 kicked off (another login took the session). The launcher
//             itself exits 42 when the shared SSO session needs owner login.
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
import readline from 'node:readline';

const EXIT_DISCONNECTED = 3;
const EXIT_FAILED = 4;
const EXIT_KICKED = 43;
// Statuses after which the SDK (autoConnect=false) holds no live channel.
const DEAD_STATUSES = new Set([-1, -3, -4]);
const KICKOFF_STATUS = -5;

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

const commands = {
  async whoami() { return runtime.handle({ action: 'get_current_uid' }); },
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
};

async function run(command) {
  const handler = commands[command.op];
  if (!handler) return { ok: false, error: { code: 'unsupported_command', message: String(command.op) } };
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
const events = sdk.instance?.CONSTANT?.MESSAGE_EVENT ?? {};

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
sdk.addEventHandler('KickOff', () => {
  write({ type: 'status', status: 'kicked', detail: 'KickOff' });
  process.exit(EXIT_KICKED);
});
sdk.addEventHandler('ConnStatusChange', (payload) => {
  const status = Number(Array.isArray(payload) ? payload[0] : (payload?.status ?? payload?.code ?? payload?.onlineStatus));
  if (status === KICKOFF_STATUS) {
    write({ type: 'status', status: 'kicked', detail: 'status -5' });
    process.exit(EXIT_KICKED);
  }
  if (DEAD_STATUSES.has(status)) {
    write({ type: 'status', status: 'disconnected', detail: `status ${status}` });
    process.exit(EXIT_DISCONNECTED);
  }
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
