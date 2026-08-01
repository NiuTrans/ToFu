"""lib/desktop_dist/platforms.py — which installer does THIS visitor need?

Pure platform/release knowledge for the desktop-download surface, extracted
from ``routes/api_v1/desktop.py`` (2026-07) so both the route AND the
background mirror (``lib/desktop_dist/mirror.py``) share ONE implementation —
the route previously owned all of this, and the mirror would have been a
second copy of the same rules (the drift class ``scripts/release_assets.py``
exists to kill).

What lives here:
  * ``_update_repo`` / ``_desktop_download_url`` — the ONE repo slug.
  * ``_platform_assets`` — the shared (os, arch, label, glob, min_bytes)
    table, loaded by path from ``scripts/release_assets.py``.
  * ``_detect_os`` / ``_detect_arch`` — UA / client-hint → platform keys.
  * ``_platform_rows_for`` — the narrowing rule (arch ambiguity fallback),
    shared by ``_match_platform_assets`` and ``store.find_for_platform``.
  * ``_assets_from_release_payload`` / ``_latest_release_assets`` — the
    pinned-tag release parser + its TTL-cached probe (kept for callers that
    legitimately want the GitHub URL list; NOT used in the status request
    path anymore — that path reads the local artifact store).
  * ``fetch_latest_release`` — the mirror's probe: tag + assets + sizes in
    one payload, no cache (the mirror owns refresh cadence).
"""

from __future__ import annotations

from lib.log import get_logger
from lib.ttl_cache import TTLCache

logger = get_logger(__name__)


def _update_repo() -> str:
    """The ``owner/name`` slug releases are published under, or ''."""
    try:
        from lib.self_update import UPDATE_REPO
    except Exception as e:
        logger.debug('[DesktopDist] UPDATE_REPO unavailable, omitting '
                     'download url: %s', e)
        return ''
    return UPDATE_REPO


def _desktop_download_url() -> str:
    """Releases page for the desktop app (the escape-hatch link)."""
    repo = _update_repo()
    return f'https://github.com/{repo}/releases/latest' if repo else ''


def _platform_assets():
    """The (os, arch, label, glob, min_bytes) table from ``scripts/release_assets.py``.

    That module is the SINGLE source of truth for which files a release must
    contain — both build-desktop.yml gates already shell out to it, and
    ``tests/test_desktop_build_workflow.py`` asserts the globs appear in no
    other file. So consumers read it rather than owning another copy.

    ``scripts/`` is not a package, so it is loaded by path. Failure is
    non-fatal: the caller degrades to the releases page.
    """
    import importlib.util
    from pathlib import Path

    global _PLATFORM_ASSETS_CACHE
    if _PLATFORM_ASSETS_CACHE is not None:
        return _PLATFORM_ASSETS_CACHE
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'release_assets.py'
    try:
        spec = importlib.util.spec_from_file_location(
            '_tofu_release_assets_route', script)
        if not spec or not spec.loader:
            raise ImportError(f'cannot load {script}')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PLATFORM_ASSETS_CACHE = tuple(mod.PLATFORM_ASSETS)
    except Exception as e:
        logger.warning('[DesktopDist] release_assets.py unreadable, falling '
                       'back to the releases page: %s', e)
        _PLATFORM_ASSETS_CACHE = ()
    return _PLATFORM_ASSETS_CACHE


_PLATFORM_ASSETS_CACHE = None

# Published asset names change only when a release is cut, so a long TTL is
# right — but it must EXPIRE, or a server that happened to probe during a
# GitHub blip would offer no direct link until restarted.
_RELEASE_ASSET_CACHE = TTLCache(ttl=900, max_size=4)


def _detect_os(user_agent: str) -> str:
    """Map a UA string to one of our three OS keys, or '' when unsure.

    Deliberately narrow. An unrecognised UA (a phone, a BSD, a bot) returns ''
    and the caller offers no direct link — sending an iPhone user a Windows
    installer is worse than showing them the releases page.

    Order matters: 'Android' contains 'Linux', and Windows UAs on ARM still
    say 'Windows NT', so the mobile checks come first.
    """
    ua = (user_agent or '').lower()
    if not ua:
        return ''
    if any(tok in ua for tok in ('android', 'iphone', 'ipad', 'ipod')):
        return ''
    if 'windows' in ua:
        return 'windows'
    if 'mac os x' in ua or 'macintosh' in ua:
        return 'macos'
    if 'linux' in ua or 'x11' in ua:
        return 'linux'
    return ''


def _detect_arch(user_agent: str, arch_hint: str) -> str:
    """Best-effort architecture, or '' when it genuinely cannot be known.

    ``arch_hint`` is the ``Sec-CH-UA-Arch`` request header (or the value the
    client resolved via ``navigator.userAgentData``). It is the ONLY honest
    source of this fact on macOS: an Apple Silicon Mac reports ``Intel Mac
    OS X`` in its UA — Chrome and Safari both do — so the UA can never
    distinguish the two DMGs. '' is a NORMAL outcome; the caller must handle
    it by offering both rather than guessing.

    The header is a structured-header string, i.e. quoted: ``"arm"``.
    """
    hint = (arch_hint or '').strip().strip('"').lower()
    if hint:
        if hint in ('arm', 'arm64', 'aarch64'):
            return 'arm64'
        if hint in ('x86', 'x86_64', 'amd64', 'x64'):
            return 'x86_64'
    ua = (user_agent or '').lower()
    # On Windows/Linux the UA does carry a usable token. Note 'arm64' must be
    # tested before the x86 tokens: a Windows-on-ARM UA contains BOTH
    # ('Windows NT 10.0; Win64; x64; ARM64'), and the ARM one is the truth.
    if 'arm64' in ua or 'aarch64' in ua:
        return 'arm64'
    if 'x86_64' in ua or 'win64' in ua or 'x64' in ua or 'amd64' in ua:
        return 'x86_64'
    return ''


def _platform_rows_for(os_key: str, arch: str = '') -> list:
    """The PLATFORM_ASSETS rows this visitor can run, arch-narrowed.

    The ONE narrowing rule, shared by ``_match_platform_assets`` (GitHub-URL
    mode) and ``store.find_for_platform`` (server-hosted mode) so the two
    supply paths can never disagree about what "the right installer" means.

    Narrowing is deliberately LOSSY-tolerant: when the detected arch has no
    build (an arm64 Windows visitor — there is no arm64 installer), the full
    OS row set is returned, because the x86_64 build runs fine under
    emulation and offering nothing is worse.
    """
    rows = [a for a in _platform_assets() if a[0] == os_key]
    if arch:
        narrowed = [a for a in rows if a[1] == arch]
        if narrowed:
            rows = narrowed
    return rows


def _assets_from_release_payload(doc, repo: str) -> list[dict]:
    """Turn a GitHub release payload into ``[{name, url, size}, …]``.

    ── The URL travels WITH the name, and that is the whole point ──
    Both facts are read out of ONE payload, so they describe one release by
    construction. Rebuilding a URL as ``/releases/latest/download/<name>``
    glues a probe-time filename to a click-time release and 404s the moment a
    new release publishes; that construction is banned here (pinned by
    tests/test_devices_panel_and_platform_download.py).

    Preference order:
      1. ``browser_download_url`` — GitHub's own pinned-tag URL.
      2. ``/releases/download/<tag_name>/<name>`` built from the SAME payload.
      3. Nothing: the asset is dropped (no honest link exists).
    """
    if not isinstance(doc, dict):
        return []
    tag = doc.get('tag_name')
    tag = tag.strip() if isinstance(tag, str) else ''
    out: list[dict] = []
    for a in doc.get('assets') or []:
        if not isinstance(a, dict):
            continue
        name = a.get('name')
        if not isinstance(name, str) or not name:
            continue
        url = a.get('browser_download_url')
        url = url.strip() if isinstance(url, str) else ''
        if not url and tag and repo:
            url = (f'https://github.com/{repo}/releases/download/'
                   f'{tag}/{name}')
        if not url:
            logger.warning('[DesktopDist] asset %s has no browser_download_url '
                           'and the payload carries no tag_name — dropping it '
                           'rather than guessing a URL', name)
            continue
        row = {'name': name, 'url': url}
        size = a.get('size')
        if isinstance(size, int) and size >= 0:
            row['size'] = size
        out.append(row)
    return out


def _latest_release_assets() -> list[dict]:
    """The newest published release's assets as ``[{name, url}, …]``, or [].

    TTL-cached; failure is non-fatal and caches an empty result so a flaky
    API cannot turn one page open into a request storm. NOT on the status
    request path anymore (that reads the local artifact store) — retained
    for callers that legitimately want the GitHub URL list.
    """
    cached = _RELEASE_ASSET_CACHE.get('assets')
    if cached is not None:
        return cached
    rows: list[dict] = []
    rel = fetch_latest_release()
    if rel:
        rows = rel['assets']
    _RELEASE_ASSET_CACHE.set('assets', rows)
    return rows


def fetch_latest_release(timeout: float = 8.0) -> dict | None:
    """Probe the newest published release: ``{'tag', 'assets'}`` or None.

    The mirror's probe. No cache — the mirror owns refresh cadence and
    freshness semantics; a cache here would let two consumers disagree about
    what "latest" is within the same minute. ``assets`` entries are
    ``{name, url, size}`` — size is what lets the mirror skip re-downloading
    an unchanged 115 MB installer every refresh.
    """
    repo = _update_repo()
    if not repo:
        return None
    try:
        from lib.http_client import http_get
        resp = http_get(
            f'https://api.github.com/repos/{repo}/releases/latest',
            timeout=timeout,
            headers={'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'})
        if resp.status_code != 200:
            logger.warning('[DesktopDist] latest-release probe returned HTTP '
                           '%s for %s', resp.status_code, repo)
            return None
        doc = resp.json()
        tag = doc.get('tag_name') if isinstance(doc, dict) else None
        assets = _assets_from_release_payload(doc, repo)
        return {'tag': (tag.strip() if isinstance(tag, str) else ''),
                'assets': assets}
    except Exception as e:
        logger.warning('[DesktopDist] latest-release probe failed: %s', e)
        return None


def _match_platform_assets(user_agent: str, arch_hint: str = '',
                           published: list | None = None) -> list[dict]:
    """The GitHub-hosted installers THIS visitor's machine can actually run.

    Retained for compatibility (and for tests that drive the narrowing with
    an injected ``published`` list). The status endpoint now serves from the
    local artifact store instead — see ``store.find_for_platform``.
    """
    import fnmatch

    repo = _update_repo()
    assets = _platform_assets()
    if not repo or not assets:
        return []
    os_key = _detect_os(user_agent)
    if not os_key:
        return []
    if published is None:
        published = _latest_release_assets()
    else:
        published = [p for p in published
                     if isinstance(p, dict) and p.get('name') and p.get('url')]
    if not published:
        return []
    arch = _detect_arch(user_agent, arch_hint)
    out = []
    for _os, _arch, label, pattern, _min_bytes in _platform_rows_for(os_key, arch):
        hit = next((a for a in published
                    if fnmatch.fnmatch(a['name'], pattern)), None)
        if not hit:
            logger.debug('[DesktopDist] no published asset matches %s', pattern)
            continue
        out.append({
            'os': _os,
            'arch': _arch,
            'label': label,
            'filename': hit['name'],
            'url': hit['url'],
        })
    return out


__all__ = [
    '_update_repo', '_desktop_download_url', '_platform_assets',
    '_PLATFORM_ASSETS_CACHE', '_RELEASE_ASSET_CACHE',
    '_detect_os', '_detect_arch', '_platform_rows_for',
    '_assets_from_release_payload', '_latest_release_assets',
    'fetch_latest_release', '_match_platform_assets',
]
