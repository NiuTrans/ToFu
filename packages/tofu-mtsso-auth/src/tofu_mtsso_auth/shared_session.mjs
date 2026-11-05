#!/usr/bin/env node
// Private pipe bridge: SDK owns login/refresh; official API owns exchange/introspection.
// No secrets in OS argv, logs, result metadata, or new MCP credential files.
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {dirname} from 'node:path';
import {pathToFileURL} from 'node:url';

const output = process.stdout.write.bind(process.stdout);
process.stdout.write = () => true;
process.stderr.write = () => true;
process.umask(0o077);
const LIMIT = 65536;
const HUMAN = new Set(['ric_feedback_required', 'sub_access_denied', 'act_access_denied',
  'sub_act_access_denied', 'MOA_NOT_LOGGED_IN', 'MOA_USER_REJECTED',
  'MOA_REJECT_COOLDOWN', 'MOA_AUTH_REQUEST_PENDING', 'SSO_SESSION_REQUIRED', 'SSO_LOGIN_NOT_APPROVED']);
const APPROVAL = '需要用户在大象中批准一次登录。';

const privateValues = new Set();
function rememberSession(data) {
  for (const [key, value] of Object.entries(data))
    if (typeof value === 'string' && /token|secret|ticket|proof|assertion|encrypted/i.test(key)) privateValues.add(value);
}
function publicMessage(value) {
  let message = String(value || '请按官方 SSO 指引处理授权。');
  for (const secret of privateValues) if (secret) message = message.split(secret).join('<redacted>');
  return message.replace(/((?:Bearer|Basic)\s+)\S+/gi, '$1<redacted>')
    .replace(/(\b(?:[a-z0-9_-]*token|[a-z0-9_-]*secret|[a-z0-9_-]*proof|client_assertion|ticket)\s*[=:]\s*)[^\s,;"'&<>]+/gi, '$1<redacted>')
    .slice(0, 1000);
}
function readBounded(fd) {
  const buffer = Buffer.alloc(LIMIT + 1);
  let offset = 0;
  while (offset < buffer.length) {
    const count = fs.readSync(fd, buffer, offset, buffer.length - offset, null);
    if (!count) break;
    offset += count;
  }
  if (offset > LIMIT) fail('SSO_INPUT_LIMIT', 'SSO 输入超过大小限制。');
  return buffer.subarray(0, offset);
}
class Failure extends Error {
  constructor(code, message, status = 1) { super(message); this.code = code; this.status = status; }
}
function fail(code, message) { throw new Failure(code, message); }
function session(path) {
  let fd;
  try {
    fd = fs.openSync(path, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  } catch (e) {
    if (e.code === 'ENOENT') return {data: {}, fingerprint: 'missing'};
    fail('SSO_SESSION_CONFIGURATION', '共享 SSO 会话不可读取。');
  }
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077) || info.size > LIMIT)
      fail('SSO_SESSION_CONFIGURATION', '共享 SSO 会话必须是当前用户的普通私有文件（0600）。');
    const raw = readBounded(fd);
    const data = JSON.parse(raw.toString('utf8'));
    if (!data || typeof data !== 'object' || Array.isArray(data))
      fail('SSO_SESSION_CONFIGURATION', '共享 SSO 会话格式无效。');
    rememberSession(data);
    return {data, fingerprint: createHash('sha256').update(raw).digest('hex')};
  } finally { fs.closeSync(fd); }
}
async function sdkWrite(path, callback) {
  // The installed SDK uses a fixed scratch name. Reserve it before any grant;
  // neither stale files nor symlinks may be overwritten by its writer.
  const parent = fs.statSync(dirname(path));
  if (!parent.isDirectory() || parent.uid !== process.getuid() || (parent.mode & 0o022))
    fail('SSO_SESSION_CONFIGURATION', '共享 SSO 源目录不可由组或其他用户写入。');
  const temporary = path + '.tmp';
  let fd;
  try {
    fd = fs.openSync(temporary, fs.constants.O_CREAT | fs.constants.O_EXCL |
      fs.constants.O_WRONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK, 0o600);
  } catch {
    fail('SSO_SESSION_CONFIGURATION', '共享 SSO 已有临时文件；请由用户检查上游会话，未覆盖原文件。');
  }
  const reserved = fs.fstatSync(fd);
  fs.closeSync(fd);
  try { return await callback(); }
  finally {
    // Remove only our own still-unrenamed reservation; preserve concurrent work.
    try {
      const current = fs.lstatSync(temporary);
      if (current.dev === reserved.dev && current.ino === reserved.ino && current.isFile()) fs.unlinkSync(temporary);
    } catch { /* SDK normally renames its scratch file into the source. */ }
  }
}
function sdkFailure(error) {
  if (error instanceof Failure) return error;
  let code = error?.code || error?.error;
  let message = error?.message || error?.error_description;
  if (!code && typeof message === 'string') {
    const structured = /^(?:CIBA 授权请求失败|获取 token 失败): ([A-Za-z][A-Za-z0-9_]{0,63}) - (.*)$/s.exec(message);
    if (structured) { code = structured[1]; message = structured[2]; }
    else if (message === '用户拒绝了授权请求') { code = 'MOA_USER_REJECTED'; message = '您已拒绝本次授权；请按官方指引处理，不重复请求。'; }
    else if (/^(?:CIBA 登录超时|授权请求已过期)/.test(message)) {
      code = 'SSO_LOGIN_NOT_APPROVED';
      message = '大象登录授权未在有效期内获批；用户确认后重试原操作即可（重试会重新发送一次授权请求）。';
    }
  }
  if (typeof code !== 'string' || !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(code))
    return new Failure('SSO_SESSION_PROVIDER_FAILED', '共享 SSO SDK 操作失败，请检查已有会话和官方配置。');
  const human = HUMAN.has(code) || code === 'access_denied' || code === 'authorization_pending';
  return new Failure(code, human ? publicMessage(message) : '共享 SSO SDK 操作失败，请检查已有会话和官方配置。', human ? 42 : 1);
}
function officialResponse(result) {
  const data = result.responseJson;
  const pending = [data];
  let visited = 0;
  while (pending.length && visited++ < 128) {
    const entry = pending.pop();
    if (!entry || typeof entry !== 'object') continue;
    for (const [key, value] of Object.entries(entry)) {
      if (value && typeof value === 'object') pending.push(value);
      else if (typeof value === 'string' && /token|secret|ticket|proof|assertion|encrypted/i.test(key)) privateValues.add(value);
    }
  }
  if (result.exitCode !== 0 || !data || typeof data !== 'object' || Array.isArray(data) ||
      data.error || data.code || data.success === false) {
    const detail = data?.error && typeof data.error === 'object' ? data.error : data;
    const code = detail?.code || (typeof data?.error === 'string' ? data.error : null) || data?.code;
    const message = detail?.message || data?.error_description || data?.message;
    // Only official human-action messages may leave the bridge; Python redacts them.
    throw new Failure(typeof code === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(code) ? code : 'SSO_EXCHANGE_FAILED',
      result.exitCode === 42 || HUMAN.has(code) ?
        (pending.length ? '官方授权诊断超过安全解析限制，请联系管理员。' : publicMessage(message)) : '官方 SSO API 调用失败。',
      result.exitCode === 42 || HUMAN.has(code) ? 42 : 1);
  }
  return data;
}
function verifiedMis(metadata, token, expected) {
  if (metadata.active !== true) fail('SSO_SESSION_REQUIRED', '共享 SSO 会话已在服务端失效，' + APPROVAL);
  let claims = {};
  // Introspection already authenticated THIS token. Decoding is only an alias lookup.
  if (typeof token === 'string' && token.split('.').length === 3) {
    try { claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8')); } catch { /* opaque is valid */ }
  }
  if ((metadata.mt_subject_type || claims.mt_subject_type) !== 'ACCOUNT' ||
      (metadata.mt_subject_type && claims.mt_subject_type && metadata.mt_subject_type !== claims.mt_subject_type))
    fail('SSO_IDENTITY_MISMATCH', '共享 SSO 需要已验证的用户身份，不能使用应用身份代替。');
  const keys = ['mis', 'login', 'subject', 'mt_mis', 'sub'];
  if (keys.some(key => metadata[key] && claims[key] && metadata[key] !== claims[key]))
    fail('SSO_IDENTITY_MISMATCH', '官方 SSO 主体与本地票据别名冲突；不能覆盖已认证主体。');
  const namedKeys = ['mis', 'login', 'mt_mis'];
  const named = namedKeys.flatMap(key => [metadata[key], claims[key]])
    .filter(value => typeof value === 'string');
  if (named.some(value => value !== expected))
    fail('SSO_IDENTITY_MISMATCH', '共享 SSO 已认证 MIS 不一致，不能继续业务调用。');
  // An opaque account sub may use a MIS alias only when the authenticated
  // introspection sub and decoded token sub agree. Never override a named user.
  for (const key of ['sub', 'subject']) {
    if (typeof metadata[key] === 'string' && metadata[key] !== expected && metadata[key] !== claims[key])
      fail('SSO_IDENTITY_MISMATCH', '官方 SSO 已认证主体不匹配，不能使用本地别名覆盖。');
  }
  const identities = keys.flatMap(key => [metadata[key], claims[key]])
    .filter(value => typeof value === 'string');
  if (!identities.includes(expected))
    fail('SSO_IDENTITY_MISMATCH', '共享 SSO 已认证主体与配置的 MIS 不一致，或缺少可验证的 MIS；不能继续业务调用。');
  return expected;
}

try {
  const raw = readBounded(0);
  const request = JSON.parse(raw.toString('utf8'));
  if (!['token', 'login'].includes(request.action) || request.environment !== 'PROD' ||
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(request.mis) ||
      !Array.isArray(request.audiences) || !request.audiences.length || request.audiences.length > 5)
    fail('SSO_SESSION_CONFIGURATION', '共享 SSO 请求配置无效。');
  const initial = session(request.session_file);
  if (initial.fingerprint !== request.fingerprint)
    fail('SSO_SESSION_CHANGED', '共享 SSO 会话已变更，请重新执行原业务操作。');
  if (initial.data.mis && initial.data.mis !== request.mis)
    fail('SSO_IDENTITY_MISMATCH', '已有共享 SSO 会话属于另一用户。');
  const upstream = await import(pathToFileURL(request.sdk).href);
  const template = upstream.ssoAuth;
  if (!template || typeof template.clientId !== 'string' || typeof template.clientSecret !== 'string')
    fail('SSO_SESSION_SDK_UNAVAILABLE', '已安装 SDK 缺少官方共享认证接口。');
  const auth = new template.constructor({clientId: template.clientId, clientSecret: template.clientSecret,
    tokenFile: request.session_file, accessEnv: template.accessEnv});
  const require = createRequire(pathToFileURL(request.sdk));
  const {runTokenExchange} = require('@mtfe/mtsso-auth-official/token-exchange');
  const {runIntrospectToken} = require('@mtfe/mtsso-auth-official/introspect-token');
  privateValues.add(template.clientSecret);
  for (const key of ['access_token', 'refresh_token']) privateValues.add(initial.data[key]);
  const credentials = {client_id: template.clientId, client_secret: template.clientSecret, env: 'PROD'};
  // `login` is the interactive business action: it reuses or renews the
  // session and requests one CIBA approval only when that is impossible.
  const interactive = request.action === 'login';
  let token = initial.data.access_token;
  let loginPerformed = false;
  async function explicitLogin() {
    loginPerformed = true;
    await sdkWrite(request.session_file, () => auth.loginWithCiba(request.mis, () => {}));
    await sdkWrite(request.session_file, () => auth.saveMis(request.mis));
    const issued = session(request.session_file).data.access_token;
    if (typeof issued !== 'string' || !issued)
      fail('SSO_SESSION_PROVIDER_FAILED', '共享 SSO SDK 登录后未写入会话。');
    return issued;
  }
  const expires = initial.data.expires_at;
  const fresh = typeof token === 'string' && token && typeof expires === 'number' &&
    Number.isFinite(expires) && Date.now() < expires - 60000;
  if (!fresh) {
    const refresh = initial.data.refresh_token;
    const refreshable = typeof refresh === 'string' && refresh;
    // Never use getAccessToken/forceLogin: those can silently enter MOA/browser login.
    token = refreshable ? await sdkWrite(request.session_file, () => auth.refreshAccessToken(refresh)) : null;
    if (!token) {
      if (!interactive)
        fail('SSO_SESSION_REQUIRED', (refreshable ? '共享 SSO 会话已过期且无法续期，' : '当前没有可用的共享 SSO 会话，') + APPROVAL);
      token = await explicitLogin();
    }
  }
  privateValues.add(token);
  let used = session(request.session_file);
  if (used.data.access_token !== token || (used.data.mis && used.data.mis !== request.mis))
    fail('SSO_SESSION_CHANGED', '共享 SSO 源会话已变更，请重新执行原业务操作。');
  let identity = officialResponse(await runIntrospectToken({...credentials, token}));
  if (identity.active !== true && interactive && !loginPerformed) {
    // A revoked but locally fresh source is repaired by one interactive
    // login. Permission/pending errors above never enter here.
    token = await explicitLogin();
    privateValues.add(token);
    used = session(request.session_file);
    if (used.data.access_token !== token || (used.data.mis && used.data.mis !== request.mis))
      fail('SSO_SESSION_CHANGED', '共享 SSO 登录来源变更，已丢弃旧结果。');
    identity = officialResponse(await runIntrospectToken({...credentials, token}));
  }
  const subject = verifiedMis(identity, token, request.mis);
  const result = officialResponse(await runTokenExchange({...credentials,
    audience: [...new Set(request.audiences)].sort().join(' '), subject_token: token}));
  if (typeof result.access_token !== 'string' || !result.access_token ||
      result.access_token.startsWith('AT_FOR_GW_BASE64_'))
    fail('SSO_TOKEN_MISSING', '官方 SSO 未返回可用用户票据。');
  const current = session(request.session_file);
  if (current.fingerprint !== used.fingerprint)
    fail('SSO_SESSION_CHANGED', '共享 SSO 源会话在换票期间变更，请重新执行原业务操作。');
  output(JSON.stringify({access_token: result.access_token, expires_in: result.expires_in,
    subject, identity_verified: true, session_fingerprint: current.fingerprint,
    login_performed: loginPerformed}) + '\n');
} catch (error) {
  // Arbitrary SDK exception text may include tickets: never project it.
  const failure = sdkFailure(error);
  output(JSON.stringify({code: failure.code, message: failure.message}) + '\n');
  process.exitCode = failure.status;
}
