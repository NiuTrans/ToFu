/** Settings desktop/mobile client presentation and browser platform projection.
 * Entry points: renderSettingsDesktopCard, renderSettingsClientInfo. Read-only
 * API completions use the common Settings session; markup policy stays here.
 */
import {featureRegistry} from '../../feature-registry';
import {escapeHtml} from '../../html-safety';
import {t} from '../../i18n';
import {currentSettingsReadSession, type SettingsReadSession} from './panel-read-session';

interface DesktopDownloadPick {
  url?: string;
  arch?: string;
  os?: string;
  label?: string;
  filename?: string;
  size?: number;
}

interface DesktopStatus {
  download_url?: string;
  downloads?: DesktopDownloadPick[];
}

interface ClientHealth {
  version?: string;
  mobile_client_url?: string;
  mobile_client_version?: string;
  ios_client_url?: string;
}
interface ClientRuntime {
  Api?: {
    desktop?: {status(arch?: string, options?: {signal?: AbortSignal}): Promise<DesktopStatus | null>};
    health?: {info(options?: {signal?: AbortSignal}): Promise<ClientHealth | null>};
  };
  apiUrl?: (path: string) => string;
  t?: (key: string) => string;
  _renderSettingsUpdatePill?: () => void;
}
const globals = () => featureRegistry as unknown as ClientRuntime;

/* Generic download-link rows for an installer card (desktop full / agent
 * Rows mirror the mobile client card's `.stg-mobile-*` chrome so both cards
 * read as ONE download component. Same payload contract as the Local Control
 * panel: `{download_url, downloads}` from /api/v4/desktop/status. The
 * releases page stays as the escape hatch for an unrecognised platform, a
 * release missing an asset, or an unreachable GitHub API. */
function stgFmtDlSize(bytes: unknown): string {
  const n = Number(bytes);
  if (!isFinite(n) || n <= 0) return '';
  const mb = n / 1048576;
  return (mb >= 100 ? Math.round(mb) : Math.round(mb * 10) / 10) + ' MB';
}

/* Re-base a server-built same-origin URL onto the CURRENT proxy base path:
 * `downloads[].url` is built from request.host_url, which a path-prefixed
 * cloud-IDE proxy strips before forwarding. Strip back to the /api/ tail
 * and re-apply the live base via apiUrl(). */
function stgResolveDlUrl(url: string): string {
  const apiUrl = globals().apiUrl;
  if (!url || typeof apiUrl !== 'function') return url;
  const i = url.indexOf('/api/');
  if (i < 0) return url;
  return apiUrl(url.slice(i));
}

/* t() with a readable fallback — the download copy must never surface a
 * raw i18n key when the locale chunk is absent (jsdom harness, stale
 * bundle). */
function stgDlT(key: 'settings.desktopRecommend' | 'settings.mobileDownload' | 'local.desktopDownloadAll', fallback: string): string {
  // Resolve through the registry (window.t in product, the harness's locale
  // translator in jsdom) so the download card honours the ACTIVE language;
  // the bundled i18n t is keyed to its own initialised locale only.
  const registryT = globals().t;
  const translate = typeof registryT === 'function' ? registryT : t;
  const v = translate(key);
  if (v && v !== key) return v;
  return fallback;
}

/* Browser architecture informs only installer presentation. macOS UA strings
 * can report Intel on Apple Silicon, so unknown stays unknown (no guessed
 * recommendation). One page-lifetime promise also coalesces pending probes. */
let stgArch: Promise<string> | null = null;

function stgResolveArch(): Promise<string> {
  if (stgArch) return stgArch;
  const browser = (typeof navigator !== 'undefined' ? navigator : null) as
    (Navigator & {userAgentData?: {getHighEntropyValues?(hints: string[]): Promise<{architecture?: string}>}}) | null;
  stgArch = Promise.resolve().then(() => browser?.userAgentData?.getHighEntropyValues?.(['architecture']))
    .then(value => value?.architecture ? String(value.architecture) : '').catch(() => '');
  return stgArch;
}

/* getHighEntropyValues answers 'arm'/'x86'; the backend's picks say
 * 'arm64'/'x86_64'. Translate between the two vocabularies. */
function stgArchMatch(arch: string): string {
  const a = (arch || '').toLowerCase();
  if (a.indexOf('arm') === 0 || a === 'aarch64') return 'arm64';
  if (a === 'x86' || a === 'ia32' || a === 'x86_64' || a === 'amd64') return 'x86_64';
  return '';
}

/* OS glyph for the platform slot — the SAME slot the mobile card gives
 * Android/iOS, so both cards read as one component. */
function stgDesktopOsIcon(osKey: string | undefined): string {
  if (osKey === 'macos') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">' +
      '<path d="M16.365 1.43c0 1.14-.46 2.2-1.21 3.02-.78.86-2.05 1.52-3.13 1.44-.14-1.1.42-2.26 1.13-3.02.79-.85 2.14-1.46 3.21-1.44zM20.94 17.1c-.57 1.3-.84 1.88-1.57 3.03-1.02 1.6-2.46 3.6-4.25 3.6-1.59.02-2-.99-4.16-.98-2.16.01-2.61 1-4.2.98-1.79-.02-3.16-1.82-4.18-3.42C.71 17.26.36 13.07 1.75 10.8c1.03-1.68 2.66-2.66 4.19-2.66 1.56 0 2.54.99 3.83.99 1.25 0 2.01-.99 3.82-.99 1.36 0 2.8.74 3.83 2.02-3.37 1.85-2.82 6.65.52 7.94z"/></svg>';
  }
  if (osKey === 'windows') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">' +
      '<path d="M3 5.7l7.2-1v6.8H3zM11.3 4.6L21 3.2v8.3h-9.7zM3 12.5h7.2v6.8l-7.2-1zM11.3 12.5H21v8.3l-9.7-1.4z"/></svg>';
  }
  if (osKey === 'linux') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3"/><path d="M12.5 15H17"/></svg>';
  }
  return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M9 21h6"/><path d="M12 17v4"/></svg>';
}

function stgDownloadLinks(d: DesktopStatus | null, arch: string): string {
  const page = ((d && d.download_url) || '').trim();
  const picks = (d && Array.isArray(d.downloads)) ? d.downloads : [];
  const want = stgArchMatch(arch);
  let html = '';
  if (picks.length) {
    html += '<div class="stg-mobile-rows">';
    for (const rawPick of picks) {
      const p = rawPick || {};
      if (!p.url) continue;
      // The recommendation matters only when there IS a choice, and is
      // shown only when the browser itself said which chip this is — the
      // label already names the arch, so a quiet badge replaces the old
      // explanatory paragraph entirely.
      const rec = picks.length > 1 && want && p.arch === want;
      html += '<a class="stg-mobile-row stg-desktop-row" href="' +
        escapeHtml(stgResolveDlUrl(p.url)) +
        '" target="_blank" rel="noopener noreferrer" title="' +
        escapeHtml(p.filename || '') + '">' +
        '<span class="stg-mobile-platform" aria-hidden="true">' +
          stgDesktopOsIcon(p.os) + '</span>' +
        '<span class="stg-mobile-name">' +
          escapeHtml(p.label || p.arch || '') + '</span>' +
        (p.size
          ? '<span class="stg-mobile-version">' +
            escapeHtml(stgFmtDlSize(p.size)) + '</span>'
          : '') +
        (rec
          ? '<span class="stg-desktop-rec">' +
            escapeHtml(stgDlT('settings.desktopRecommend', '推荐')) + '</span>'
          : '') +
        '<span class="stg-mobile-get">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M6 11l6 6 6-6"/><path d="M5 21h14"/></svg>' +
          '<span>' + escapeHtml(stgDlT('settings.mobileDownload', '下载')) +
          '</span></span></a>';
    }
    html += '</div>';
  }
  if (page) {
    html += '<a class="stg-desktop-all" href="' +
      escapeHtml(page) + '" target="_blank" rel="noopener noreferrer">' +
      escapeHtml(stgDlT('local.desktopDownloadAll', '查看全部下载 ↗')) + '</a>';
  }
  return html;
}

/* The full Windows/macOS desktop clients are a download, not a control —
 * they live in Settings → 关于与更新, next to the mobile client card.
 * Config-gated like the mobile card: hidden until /api/v4/desktop/status
 * answers with at least a releases page. The agent (受控端) stays in Local
 * Control; it is the thing that controls the machine. */
export function renderSettingsDesktopCard(session = currentSettingsReadSession()): void {
  const card = document.getElementById('settingsDesktopCard');
  if (!card) return;
  const desktop = globals().Api?.desktop;
  if (!desktop || typeof desktop.status !== 'function') {
    card.style.display = 'none';
    return;
  }
  void Promise.all([stgResolveArch(), Promise.resolve().then(() => !session || session.isCurrent() ? desktop.status('', {signal: session?.scope.signal}) : null)])
    .then((res) => {
      if (session && !session.isCurrent() || document.getElementById('settingsDesktopCard') !== card) return;
      const d = res[1];
      if (!d || (!d.download_url && !(d.downloads || []).length)) {
        card.style.display = 'none';
        return;
      }
      const rows = document.getElementById('settingsDesktopRows');
      if (rows) rows.innerHTML = stgDownloadLinks(d, res[0]);
      card.style.display = '';
    }).catch(() => {
      if ((!session || session.isCurrent()) && document.getElementById('settingsDesktopCard') === card) card.style.display = 'none';
    });
}

function projectMobileClient(data: ClientHealth | null | undefined): void {
  const card = document.getElementById('settingsMobileCard');
  if (!card) return;
  const android = document.getElementById('settingsMobileAndroid');
  if (android instanceof HTMLAnchorElement) {
    if (data?.mobile_client_url) android.href = data.mobile_client_url;
    else android.removeAttribute('href');
  }
  const version = document.getElementById('settingsMobileAndroidVersion');
  if (version) {
    version.textContent = data?.mobile_client_version ? 'v' + data.mobile_client_version : '';
    version.style.display = data?.mobile_client_version ? '' : 'none';
  }
  const ios = document.getElementById('settingsMobileIos');
  if (ios instanceof HTMLAnchorElement) {
    const url = data?.ios_client_url;
    if (url) { ios.href = url; ios.target = '_blank'; }
    else { ios.removeAttribute('href'); ios.removeAttribute('target'); }
    ios.classList.toggle('stg-mobile-row-soon', !url);
    const badge = document.getElementById('settingsMobileIosBadge');
    if (badge) badge.style.display = url ? 'none' : '';
  }
  card.style.display = data?.mobile_client_url ? '' : 'none';
}

export function renderSettingsClientInfo(session: SettingsReadSession): void {
  const bridge = globals();
  void Promise.resolve().then(() => session.isCurrent()
    ? bridge.Api?.health?.info({signal: session.scope.signal}) : null).then(data => {
    if (!session.isCurrent()) return;
    const version = document.getElementById('settingsVersion');
    if (version) version.textContent = data?.version ? 'v' + data.version : '';
    const updateVersion = document.getElementById('settingsUpdateVersion');
    if (updateVersion) updateVersion.textContent = data?.version
      ? t('settings.updateCurrent', {version: data.version}) : '';
    bridge._renderSettingsUpdatePill?.();
    projectMobileClient(data);
  }).catch(() => { /* Health is optional; preserve the last confirmed release info. */ });
}
