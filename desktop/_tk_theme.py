"""Single theme source for every Tofu tkinter dialog.

Both native dialogs (first-launch component picker, remote-connect prompt)
used to be stock gray ttk with hardcoded English strings and ad-hoc colors
(``foreground='gray'`` / ``'#666'`` / ``'#b00'``) — a different universe
from the web UI's design-token system sitting one process away. This module
is the ONE place that owns:

* **Palettes** — LIGHT and DARK, derived from the web UI's own ``:root``
  tokens (static/styles.css), so the native chrome and the web UI read as
  one product. Identical key sets: a token added to one palette and not
  the other fails the palette parity test instead of KeyErroring a dialog.
* **Dark-mode detection** — TOFU_THEME override → Windows registry →
  macOS ``defaults`` → Linux ``gsettings``; every failure shape falls back
  to LIGHT (a wrong-light dialog is readable; a crashed one is not).
* **Bilingual strings** — zh/en by TOFU_LANG → OS locale → env; every key
  carries both languages (the web UI is fully bilingual; native chrome was
  the only English-only surface left).
* **ttk style application** — clam base + named ``Tofu.*`` styles, plus
  logo loading and DPI awareness (3-level fallback, moved here from the
  two private copies that had already diverged).

Rules for consumers: never hardcode a color, a font size family, or a
user-facing string in a dialog — add a token/key HERE and reference it.

Headless rule: tkinter is imported LAZILY inside functions (never at module
level) so tests can import this module on a display-less CI runner.
"""

import locale
import os
import subprocess
import sys

# ═══════════════════════════════════════════════════════════════
#  Palettes — from static/styles.css :root / [data-theme="light"]
# ═══════════════════════════════════════════════════════════════

DARK = {
    'bg': '#0a0a0c',
    'bg2': '#111115',
    'bg3': '#1a1a21',
    'hover': '#22222b',
    'active': '#2a2a35',
    'border': '#2a2a35',
    'border_light': '#33333f',
    'text': '#e8e8ed',
    'text2': '#9898a8',
    'text3': '#6a6a7a',
    'accent': '#6e56cf',
    'accent_hover': '#7c66d4',
    'accent_fg': '#ffffff',
    'success': '#10b981',
    'error': '#cf5050',
}

LIGHT = {
    'bg': '#f4f2ed',
    'bg2': '#eceae4',
    'bg3': '#e3e1da',
    'hover': '#d9d7cf',
    'active': '#d0cdc4',
    'border': '#c2beb3',
    'border_light': '#ada89c',
    'text': '#272732',
    'text2': '#504f5b',
    'text3': '#868490',
    'accent': '#6366f1',
    'accent_hover': '#4f46e5',
    'accent_fg': '#ffffff',
    'success': '#15803d',
    'error': '#b82020',
}


def detect_dark() -> bool:
    """True when the OS is in dark mode. Light fallback on ANY failure.

    Order: explicit TOFU_THEME override (wins over the OS probe, and costs
    no subprocess), then the per-platform probe. Never raises.
    """
    override = (os.environ.get('TOFU_THEME') or '').strip().lower()
    if override in ('dark', 'light'):
        return override == 'dark'
    try:
        if sys.platform.startswith('win'):
            # HKCU\...\Themes\Personalize:AppsUseLightTheme — 0 means dark.
            import winreg  # noqa: PLC0415 — Windows-only module, lazy by design
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
            try:
                value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
            finally:
                winreg.CloseKey(key)
            return value == 0
        if sys.platform == 'darwin':
            # The key EXISTS only in dark mode — exit 1 means light.
            r = subprocess.run(
                ['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                capture_output=True, text=True, timeout=2)
            return r.returncode == 0 and 'dark' in (r.stdout or '').lower()
        if sys.platform.startswith('linux'):
            # GNOME 42+ color-scheme; absent gsettings/old GNOME → light.
            r = subprocess.run(
                ['gsettings', 'get', 'org.gnome.desktop.interface',
                 'color-scheme'],
                capture_output=True, text=True, timeout=2)
            return 'dark' in (r.stdout or '').lower()
    except Exception:
        return False
    return False


def current_palette() -> dict:
    """The DARK or LIGHT token dict for the current OS state."""
    return DARK if detect_dark() else LIGHT


# ═══════════════════════════════════════════════════════════════
#  Font stacks — never let tk pick the family by itself
# ═══════════════════════════════════════════════════════════════
# The old dialogs styled everything with the empty-family tuple ('', 10).
# On a Chinese-locale Windows that resolves through the system default GUI
# font to SimSun (宋体) — a SERIF face — so the entire native surface
# rendered in serifs (owner report 2026-08-04). Pick an explicit family at
# runtime: the first stack entry tk actually has, CJK-first when the UI
# language is zh, '' (= tk default) only as the last resort.
_FONT_STACKS = {
    'win': ('Segoe UI', 'Microsoft YaHei UI', 'Tahoma', 'Arial'),
    'win_zh': ('Microsoft YaHei UI', 'Segoe UI', 'Tahoma', 'Arial'),
    'darwin': ('Helvetica Neue', 'PingFang SC', 'Lucida Grande', 'Arial'),
    'darwin_zh': ('PingFang SC', 'Helvetica Neue', 'Lucida Grande', 'Arial'),
    'linux': ('Noto Sans', 'DejaVu Sans', 'Liberation Sans', 'Arial'),
    'linux_zh': ('Noto Sans CJK SC', 'WenQuanYi Micro Hei', 'Noto Sans',
                 'DejaVu Sans', 'Arial'),
}


def pick_font_family(available, platform=None, lang=None) -> str:
    """First stack family present in ``available``, else '' (tk default).

    Pure — takes the family list as an argument so headless tests drive it
    without a display. ``platform``/``lang`` default to the real probes.
    """
    platform = platform if platform is not None else sys.platform
    lang = lang or detect_lang()
    if platform.startswith('win'):
        key = 'win_zh' if lang == 'zh' else 'win'
    elif platform == 'darwin':
        key = 'darwin_zh' if lang == 'zh' else 'darwin'
    else:
        key = 'linux_zh' if lang == 'zh' else 'linux'
    avail = {str(f).lower() for f in available}
    for family in _FONT_STACKS[key]:
        if family.lower() in avail:
            return family
    return ''


def _center_geometry(screen_w, screen_h, win_w, win_h) -> str:
    """'+x+y' placing the window optically centred (slightly above middle).

    Pure math, headless-testable; clamps so a window larger than the
    screen never opens with its title bar off the top edge.
    """
    x = max(0, (int(screen_w) - int(win_w)) // 2)
    y = max(0, int((int(screen_h) - int(win_h)) * 0.38))
    return '+%d+%d' % (x, y)


def center_on_screen(root, width=None) -> None:
    """Move ``root`` to the screen centre at its requested (or given) size.

    Best-effort: a window in the wrong place is still a window, so every
    failure is swallowed.
    """
    try:
        root.update_idletasks()
        w = int(width or root.winfo_reqwidth())
        h = int(root.winfo_reqheight())
        root.geometry('%dx%d%s' % (w, h, _center_geometry(
            root.winfo_screenwidth(), root.winfo_screenheight(), w, h)))
    except Exception:
        pass


def set_window_icon(root) -> None:
    """Brand the window's title bar / taskbar entry (best-effort).

    iconphoto works everywhere tk does; iconbitmap with the bundled .ico
    additionally fixes the Windows taskbar/alt-tab icon (the tk-feather
    default was part of the「窗口最小化后找不到」confusion).
    """
    try:
        photo = load_logo_photo(root, size=64)
        if photo is not None:
            root.iconphoto(True, photo)
    except Exception:
        pass
    if not sys.platform.startswith('win'):
        return
    try:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if getattr(sys, 'frozen', False):
            bundle = os.path.join(os.path.dirname(sys.executable),
                                  '_internal')
            if os.path.isdir(bundle):
                base = bundle
        ico = os.path.join(base, 'static', 'icons', 'tofu.ico')
        if os.path.isfile(ico):
            root.iconbitmap(ico)
    except Exception:
        pass


def detect_lang() -> str:
    """'zh' or 'en'. TOFU_LANG wins, then the OS locale, then LANG-family
    env vars; 'en' when nothing says otherwise. Never raises.

    Windows gotcha (owner report 2026-08-04 — a zh-CN machine rendered the
    whole native surface in English): ``locale.getlocale()`` there returns
    DISPLAY names, not ISO codes — ``'Chinese (Simplified)_China'`` /
    ``'Chinese (Traditional)_Taiwan'`` — which do not start with ``'zh'``,
    so the old code hard-fell to English. Normalise (lowercase; dashes and
    spaces to underscores) and accept both the ISO prefix and the Windows
    display-name prefix.
    """
    override = (os.environ.get('TOFU_LANG') or '').strip().lower()
    if override.startswith('zh'):
        return 'zh'
    if override.startswith('en'):
        return 'en'
    try:
        loc = locale.getlocale()[0] or ''
    except Exception:
        loc = ''
    norm = loc.strip().lower().replace('-', '_').replace(' ', '_')
    if norm.startswith('zh') or norm.startswith('chinese'):
        return 'zh'
    if loc:
        return 'en'
    for var in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        v = (os.environ.get(var) or '').strip().lower()
        if v:
            return 'zh' if v.startswith('zh') else 'en'
    return 'en'


# ═══════════════════════════════════════════════════════════════
#  Strings — every user-facing native-chrome string, both languages
# ═══════════════════════════════════════════════════════════════

STRINGS = {
    'desktop.components.title': {
        'en': 'Optional Components',
        'zh': '可选组件',
    },
    'desktop.components.subtitle': {
        'en': 'These components can be downloaded now or later. '
              'Recommended components are pre-selected.',
        'zh': '以下组件可以现在下载，也可以稍后通过系统托盘安装。推荐组件已默认勾选。',
    },
    'desktop.components.install': {
        'en': 'Install Selected',
        'zh': '安装所选',
    },
    'desktop.components.skip': {
        'en': 'Skip for now',
        'zh': '暂不安装',
    },
    'desktop.components.close': {
        'en': 'Close',
        'zh': '关闭',
    },
    'desktop.components.pending': {
        'en': 'Pending',
        'zh': '等待中',
    },
    'desktop.components.installing': {
        'en': 'Installing…',
        'zh': '安装中…',
    },
    'desktop.components.installed': {
        'en': 'Installed',
        'zh': '已安装',
    },
    'desktop.components.alreadyInstalled': {
        'en': 'Already installed.',
        'zh': '已安装。',
    },
    'desktop.components.failed': {
        'en': 'Failed',
        'zh': '失败',
    },
    'desktop.components.summaryOk': {
        'en': 'All selected components are installed.',
        'zh': '所选组件已全部安装完成。',
    },
    'desktop.components.summaryFail': {
        'en': '{n} component(s) failed — details above. '
              'You can retry from the tray menu.',
        'zh': '有 {n} 个组件安装失败，详见上方信息。可稍后从系统托盘菜单重试。',
    },
    'desktop.connect.title': {
        'en': 'Connect to remote Tofu',
        'zh': '连接到远程 Tofu',
    },
    'desktop.connect.heading': {
        'en': 'Connect this computer to a remote Tofu',
        'zh': '将这台电脑连接到远程 Tofu',
    },
    'desktop.connect.instructions': {
        'en': 'In Tofu, open Local Control → This computer and press '
              '"Pair this computer" — the 6-digit code needs no address. '
              'This line is the advanced fallback: paste it whole.',
        'zh': '在 Tofu 中打开「本机控制 → 这台电脑」，优先点「配对这台电脑」'
              '用 6 位码配对（无需地址）。连接行是高级兜底：将整行粘贴到这里。',
    },
    'desktop.connect.current': {
        'en': 'Currently attached to: {url}',
        'zh': '当前已连接到：{url}',
    },
    'desktop.connect.cancel': {
        'en': 'Cancel',
        'zh': '取消',
    },
    'desktop.connect.connect': {
        'en': 'Connect',
        'zh': '连接',
    },
    'desktop.connect.verifying': {
        'en': 'Verifying the server address…',
        'zh': '正在验证服务器地址…',
    },
    'desktop.connect.verifyFailed': {
        'en': 'Cannot reach Tofu there: {reason}. An agent cannot use a '
              'proxy/SSO address — prefer the pairing code (the agent '
              'discovers the route itself). Press Connect again to save '
              'anyway.',
        'zh': '连不上服务器：{reason}。代理/SSO 地址受控端用不了——建议改用'
              '配对码（通路由受控端自己发现）；再点一次「连接」强制保存。',
    },
    'desktop.pair.title': {
        'en': 'Pair this computer',
        'zh': '配对这台电脑',
    },
    'desktop.pair.heading': {
        'en': 'Pair this computer with Tofu',
        'zh': '将这台电脑与 Tofu 配对',
    },
    'desktop.pair.instructions': {
        'en': 'In Tofu, open Local Control → This computer and press '
              '"Pair this computer", then type the 6-digit code below. '
              'The server address is pre-filled — edit it only if it is '
              'wrong.',
        'zh': '在 Tofu 面板「本机控制 → 这台电脑」点「配对这台电脑」，'
              '把 6 位配对码填到下面。服务器地址已自动填好，不对再改。',
    },
    'desktop.pair.serverLabel': {
        'en': 'Server address',
        'zh': '服务器地址',
    },
    'desktop.pair.codeLabel': {
        'en': 'Pairing code',
        'zh': '配对码',
    },
    'desktop.pair.connect': {
        'en': 'Pair and connect',
        'zh': '配对并连接',
    },
    'desktop.pair.useLine': {
        'en': 'Use a connect line instead…',
        'zh': '改用连接行…',
    },
    'desktop.pair.cancel': {
        'en': 'Cancel',
        'zh': '取消',
    },
    'desktop.pair.verifying': {
        'en': 'Pairing…',
        'zh': '正在配对…',
    },
    'desktop.pair.badAddress': {
        'en': 'The server address must start with http:// or https://.',
        'zh': '服务器地址必须以 http:// 或 https:// 开头。',
    },
    'desktop.pair.badCode': {
        'en': 'The pairing code is the 6-digit number shown in the panel.',
        'zh': '配对码是面板生成的 6 位数字。',
    },
    'desktop.pair.invalidCode': {
        'en': 'This code is invalid, expired or already used — mint a '
              'fresh one in the panel.',
        'zh': '配对码无效、过期或已被使用——请回面板重新生成。',
    },
    'desktop.pair.rateLimited': {
        'en': 'Too many failed attempts from this address. Wait a few '
              'minutes and try again with a fresh code.',
        'zh': '此地址失败次数过多——请等几分钟，用新配对码重试。',
    },
    'desktop.pair.failed': {
        'en': 'Could not pair at that address: {reason}. Check the '
              'server address (a proxy/SSO gateway cannot be used).',
        'zh': '无法在该地址完成配对：{reason}。请检查服务器地址'
              '（代理/SSO 地址不可用）。',
    },
    'desktop.comp.postgresql.name': {
        'en': 'PostgreSQL Database',
        'zh': 'PostgreSQL 数据库',
    },
    'desktop.comp.postgresql.desc': {
        'en': 'High-performance database: better concurrency, JSONB, '
              'full-text search. Without it the app uses SQLite '
              '(single-user, still fully functional).',
        'zh': '高性能数据库：更好的并发、JSONB 与全文检索。'
              '不安装则使用 SQLite（单用户场景同样完整可用）。',
    },
    'desktop.comp.chromium.name': {
        'en': 'Browser Engine (Chromium)',
        'zh': '浏览器引擎（Chromium）',
    },
    'desktop.comp.chromium.desc': {
        'en': 'Enables fetching JavaScript-rendered pages and browser '
              'automation. Required for fetch_url on JS-heavy sites.',
        'zh': '支持抓取需要 JavaScript 渲染的网页与浏览器自动化，'
              '是 fetch_url 应对重 JS 站点的前提。',
    },
    # ── System-tray strings (pystray menus of BOTH launchers) ──
    # The tray was the last English-only surface: every MenuItem literal was
    # hardcoded. tests/test_desktop_tray_i18n.py AST-ratchets both launchers
    # so a literal can never come back. Placeholder tokens ({tag}/{url}/
    # {port}) are filled by the call sites via .replace().
    'desktop.tray.open': {
        'en': 'Open Tofu',
        'zh': '打开 Tofu',
    },
    'desktop.tray.downloadUpdate': {
        'en': 'Download update ({tag})',
        'zh': '下载更新（{tag}）',
    },
    'desktop.tray.enableCC': {
        'en': 'Enable Computer Control',
        'zh': '启用电脑控制',
    },
    'desktop.tray.permissions': {
        'en': 'Permissions',
        'zh': '权限',
    },
    'desktop.tray.permWrite': {
        'en': 'Allow file writes',
        'zh': '允许写入文件',
    },
    'desktop.tray.permExec': {
        'en': 'Allow run commands / open apps',
        'zh': '允许运行命令 / 打开应用',
    },
    'desktop.tray.permGui': {
        'en': 'Allow mouse / keyboard / screenshot',
        'zh': '允许鼠标 / 键盘 / 截图',
    },
    'desktop.tray.permEgress': {
        'en': 'Allow relaying subscription API traffic',
        'zh': '允许转发订阅 API 流量',
    },
    'desktop.tray.connectRemote': {
        'en': 'Connect to remote Tofu…',
        'zh': '连接到远程 Tofu…',
    },
    'desktop.tray.connectDifferent': {
        'en': 'Connect to a different Tofu…',
        'zh': '连接到另一个 Tofu…',
    },
    'desktop.tray.installComponents': {
        'en': 'Install Components...',
        'zh': '安装组件...',
    },
    'desktop.tray.serverLabel': {
        'en': 'Server: {url}',
        'zh': '服务器：{url}',
    },
    'desktop.tray.serverLocal': {
        'en': 'this computer (port {port})',
        'zh': '本机（端口 {port}）',
    },
    'desktop.tray.notAttached': {
        'en': '(not attached)',
        'zh': '（未连接）',
    },
    'desktop.tray.autostart': {
        'en': 'Start with Windows',
        'zh': '开机自启',
    },
    'desktop.tray.quit': {
        'en': 'Quit',
        'zh': '退出',
    },
    'desktop.tray.controlPanel': {
        'en': 'Control panel…',
        'zh': '控制面板…',
    },
    'desktop.tray.linkState': {
        'en': 'Link: {status}',
        'zh': '链路：{status}',
    },
    'desktop.tray.stOk': {
        'en': 'connected',
        'zh': '已连上',
    },
    'desktop.tray.stAuth': {
        'en': 'auth failed — the credential is dead; re-pair via '
              '"Connect to a different Tofu…"',
        'zh': '鉴权失败——密钥已失效；用托盘「连接到另一个 Tofu…」重新配对即可',
    },
    'desktop.tray.stProxy': {
        'en': 'blocked by a proxy/SSO gateway — re-discovering the route '
              '(auto-tunnel included)',
        'zh': '地址被代理/SSO 拦截——正在自动重找通路（含自动隧道）',
    },
    'desktop.tray.stUnreachable': {
        'en': 'server unreachable — retrying and re-discovering the route '
              'by itself',
        'zh': '连不上服务器——正在自动重试并重找通路',
    },
    'desktop.tray.stHttp': {
        'en': 'server answered HTTP {code}',
        'zh': '服务器返回 HTTP {code}',
    },
    'desktop.tray.stStarting': {
        'en': 'connecting…',
        'zh': '连接中…',
    },
    # ── Startup role window (desktop/role_window.py) ──
    # Both launchers show this window at startup so the machine's ROLE is
    # never implicit again; it doubles as the control panel that used to
    # be tray-only.
    'desktop.role.serverTitle': {
        'en': 'This computer runs your Tofu server',
        'zh': '这台电脑是 Tofu 服务器',
    },
    'desktop.role.serverSub': {
        'en': 'Tofu is running locally. The web app opens in your '
              'browser automatically.',
        'zh': 'Tofu 正在本机运行，网页版会自动在浏览器中打开。',
    },
    'desktop.role.alsoClient': {
        'en': 'Also controlled by: {url}',
        'zh': '同时受控于：{url}',
    },
    'desktop.role.agentTitle': {
        'en': 'This computer is controlled by a Tofu server',
        'zh': '这台电脑是 Tofu 受控端',
    },
    'desktop.role.agentSub': {
        'en': 'It answers to the server below. You can change '
              'permissions or re-attach at any time.',
        'zh': '它听命于下方服务器，你可以随时修改权限或重新连接。',
    },
    'desktop.role.ccTitle': {
        'en': 'Computer control',
        'zh': '电脑控制',
    },
    'desktop.role.ccOn': {
        'en': 'Enabled — remote control is ON',
        'zh': '已启用——远程控制开启中',
    },
    'desktop.role.ccOff': {
        'en': 'Disabled — this computer cannot be controlled',
        'zh': '未启用——这台电脑不可被控制',
    },
    'desktop.role.enable': {
        'en': 'Enable',
        'zh': '启用',
    },
    'desktop.role.disable': {
        'en': 'Disable',
        'zh': '停用',
    },
    'desktop.role.permHint': {
        'en': 'Permission tiers apply on the next command poll.',
        'zh': '权限档位在下一次指令轮询时生效。',
    },
    'desktop.role.showAtStartup': {
        'en': 'Show this window at startup',
        'zh': '启动时显示此窗口',
    },
    'desktop.role.minimize': {
        'en': 'Minimize to tray',
        'zh': '最小化到托盘',
    },
    'desktop.role.serverCardLabel': {
        'en': 'SERVER',
        'zh': '服务器',
    },
    # One-line tier explanations — the tray never had room for these, so a
    # tier like「转发订阅 API 流量」was guesswork. The window has the room.
    'desktop.role.tierWriteDesc': {
        'en': 'Create, edit and delete files on this machine.',
        'zh': '在本机创建、修改和删除文件。',
    },
    'desktop.role.tierExecDesc': {
        'en': 'Run shell commands and open applications.',
        'zh': '运行命令、打开应用程序。',
    },
    'desktop.role.tierGuiDesc': {
        'en': 'Move the mouse, type and capture the screen.',
        'zh': '操作鼠标键盘、截取屏幕画面。',
    },
    'desktop.role.tierEgressDesc': {
        'en': 'Relay subscription API traffic through this machine.',
        'zh': '经本机转发订阅 API 流量。',
    },
    'desktop.role.autostartDesc': {
        'en': 'Launch the agent automatically when you sign in.',
        'zh': '登录系统时自动启动受控端。',
    },
    # ── Machine-token → phrase mapping (the 2026-08-04 i18n sweep) ──
    # lib modules (probe/pair/config/post_install workers) return MACHINE
    # TOKENS, never prose; the UI boundary maps them HERE so a Chinese
    # dialog never shows a raw English token. Unknown tokens pass through
    # verbatim (a new token is visible in dev, and the census ratchet in
    # tests/test_desktop_native_i18n.py demands a key for it).
    'desktop.reason.unreachable': {
        # No 'reach'/'连' here — the shells already say「连不上 / Cannot
        # reach」, and the composed sentence must not double the verb on
        # the most-read failure copy in the product (owner final polish).
        'en': 'no answer from the server',
        'zh': '服务器无响应',
    },
    'desktop.reason.timeout': {
        'en': 'the connection timed out',
        'zh': '连接超时',
    },
    'desktop.reason.error': {
        'en': 'a network error occurred',
        'zh': '发生网络错误',
    },
    'desktop.reason.notTofu': {
        'en': 'the address answers but is not a Tofu server',
        'zh': '该地址可达，但不是 Tofu 服务器',
    },
    'desktop.reason.badResponse': {
        'en': 'the server returned an unreadable response',
        'zh': '服务器返回了无法理解的响应',
    },
    'desktop.reason.http': {
        'en': 'the server answered HTTP {code}',
        'zh': '服务器返回 HTTP {code}',
    },
    # parse_connect_line refusals (ConnectLineError.code → message).
    'desktop.connect.errMissingParts': {
        'en': 'Paste the whole line from Tofu — it must contain the server '
              'address AND the token, separated by a space.',
        'zh': '请粘贴 Tofu 给出的完整一行——必须同时包含服务器地址和令牌，'
              '以空格分隔。',
    },
    'desktop.connect.errTooManyParts': {
        'en': 'That looks like more than one server address and token. '
              'Paste exactly the line Tofu showed you.',
        'zh': '内容看起来多于一组地址和令牌——请只粘贴 Tofu 给出的那一整行。',
    },
    'desktop.connect.errBadUrl': {
        'en': 'The server address must start with http:// or https:// — '
              'got {detail}.',
        'zh': '服务器地址必须以 http:// 或 https:// 开头——当前为「{detail}」。',
    },
    # Component-card size hints (were hardcoded English attributes).
    'desktop.comp.chromium.size': {
        'en': '~115 MB download',
        'zh': '需下载约 115 MB',
    },
    'desktop.comp.postgresql.size': {
        'en': '~50 MB download',
        'zh': '需下载约 50 MB',
    },
    # Component worker messages (install() + progress_callback tokens).
    'desktop.compmsg.chromiumOk': {
        'en': 'Chromium browser installed successfully.',
        'zh': 'Chromium 浏览器安装成功。',
    },
    'desktop.compmsg.chromiumTimeout': {
        'en': 'Download timed out (10 min). Check your network connection.',
        'zh': '下载超时（10 分钟）——请检查网络连接。',
    },
    'desktop.compmsg.chromiumNoModule': {
        'en': 'Playwright module not found in bundle.',
        'zh': '安装包内缺少 Playwright 模块。',
    },
    'desktop.compmsg.chromiumDownloading': {
        'en': 'Downloading Chromium…',
        'zh': '正在下载 Chromium…',
    },
    'desktop.compmsg.pgOk': {
        'en': 'PostgreSQL configured successfully.',
        'zh': 'PostgreSQL 配置完成。',
    },
    'desktop.compmsg.pgBootstrapFailed': {
        'en': 'PostgreSQL bootstrap failed. The app will use SQLite '
              'instead. You can install PostgreSQL manually later.',
        'zh': 'PostgreSQL 引导失败——应用将改用 SQLite；也可以稍后手动安装 '
              'PostgreSQL。',
    },
    'desktop.compmsg.pgNoModule': {
        'en': 'Database bootstrap module not available. PostgreSQL can be '
              'installed manually.',
        'zh': '数据库引导模块不可用——可手动安装 PostgreSQL。',
    },
    'desktop.compmsg.pgSettingUp': {
        'en': 'Setting up PostgreSQL…',
        'zh': '正在配置 PostgreSQL…',
    },
    # Dynamic worker details (stderr etc.) stay raw but get a localized
    # prefix — the owner rule for the sweep.
    'desktop.components.failedDetail': {
        'en': 'Installation failed',
        'zh': '安装失败',
    },
    # The tray link line's error branch (detail is the raw exception).
    'desktop.tray.stError': {
        'en': 'error — {detail}',
        'zh': '出错——{detail}',
    },
    # The no-tkinter terminal fallback's interactive lines.
    'desktop.terminal.installPrompt': {
        'en': '  Install {name} ({size})? [{default}/{other}]: ',
        'zh': '  安装 {name}（{size}）？[{default}/{other}]: ',
    },
    'desktop.terminal.alreadyInstalled': {
        'en': '  [OK] {name} — already installed',
        'zh': '  [OK] {name}——已安装',
    },
}


def t(key: str, lang: str = None) -> str:
    """Look up a string in the current (or given) language.

    Missing key → the key itself (visible in dev, never a crash); missing
    translation → English.
    """
    lang = lang or detect_lang()
    pair = STRINGS.get(key)
    if not pair:
        return key
    return pair.get(lang) or pair.get('en') or key


# ═══════════════════════════════════════════════════════════════
#  Machine-token → localized text (lib returns tokens; the UI maps)
# ═══════════════════════════════════════════════════════════════

_REASON_KEYS = {
    'unreachable': 'desktop.reason.unreachable',
    'timeout': 'desktop.reason.timeout',
    'error': 'desktop.reason.error',
    'not_tofu': 'desktop.reason.notTofu',
    'bad_response': 'desktop.reason.badResponse',
}

_CONNECT_ERROR_KEYS = {
    'missing_parts': 'desktop.connect.errMissingParts',
    'too_many_parts': 'desktop.connect.errTooManyParts',
    'bad_url': 'desktop.connect.errBadUrl',
}

_COMP_MSG_KEYS = {
    'chromium_ok': 'desktop.compmsg.chromiumOk',
    'chromium_timeout': 'desktop.compmsg.chromiumTimeout',
    'chromium_no_module': 'desktop.compmsg.chromiumNoModule',
    'chromium_downloading': 'desktop.compmsg.chromiumDownloading',
    'pg_ok': 'desktop.compmsg.pgOk',
    'pg_bootstrap_failed': 'desktop.compmsg.pgBootstrapFailed',
    'pg_no_module': 'desktop.compmsg.pgNoModule',
    'pg_setting_up': 'desktop.compmsg.pgSettingUp',
}


def reason_text(token, lang=None) -> str:
    """Map a probe/pair machine token to a localized short phrase.

    ``http_404`` fills the {code} placeholder of a shared key; an unknown
    token passes through verbatim (visible in dev, never a crash — and the
    census ratchet demands a key for every token the lib can emit).
    """
    tok = str(token or '')
    if tok.startswith('http_'):
        return t('desktop.reason.http', lang).replace('{code}', tok[5:])
    key = _REASON_KEYS.get(tok)
    return t(key, lang) if key else tok


def connect_error_text(err, lang=None) -> str:
    """Map a ConnectLineError to its localized dialog message.

    Unknown/legacy ValueErrors (no ``code``) pass through str() — the
    refusal must never be swallowed, coded or not.
    """
    key = _CONNECT_ERROR_KEYS.get(getattr(err, 'code', ''))
    if not key:
        return str(err)
    return t(key, lang).replace('{detail}',
                                getattr(err, 'detail', '') or '—')


def component_msg(msg, lang=None) -> str:
    """Map a component-worker message to localized text.

    Tokens (``chromium_timeout`` …) map to keys; ``detail:<raw>`` keeps the
    raw tail (a stderr excerpt) behind a localized「安装失败」prefix — the
    owner rule for dynamic details; anything else passes through.
    """
    s = str(msg or '')
    if s.startswith('detail:'):
        return '%s: %s' % (t('desktop.components.failedDetail', lang),
                           s[7:].strip())
    key = _COMP_MSG_KEYS.get(s)
    return t(key, lang) if key else s


# ═══════════════════════════════════════════════════════════════
#  tk application — LAZY imports (headless CI must import this file)
# ═══════════════════════════════════════════════════════════════

def ensure_dpi_awareness(log=lambda msg: None) -> None:
    """Mark the process per-monitor DPI-aware so HiDPI rendering is crisp.

    THE 3-level fallback (Per-Monitor v2 → Per-Monitor → System), moved here
    from launcher.py / post_install.py whose two private copies had already
    diverged (the post_install one lacked the v2 level). Must run before any
    window is created. No-op off Windows. Never raises.
    """
    if not sys.platform.startswith('win'):
        return
    try:
        import ctypes
    except Exception as e:  # pragma: no cover
        log('DPI awareness unavailable: %s' % e)
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4 (Win 10 1703+)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Win 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        log('Could not set DPI awareness: %s' % e)


def apply_theme(root, palette=None) -> dict:
    """Theme a tk root: background + clam-based ``Tofu.*`` ttk styles.

    Returns the palette in use so the dialog can color non-ttk widgets
    (tk Frames used as cards, the window itself).
    """
    import tkinter as tk  # noqa: PLC0415 — lazy, headless rule
    from tkinter import ttk

    p = palette or current_palette()
    root.configure(bg=p['bg'])

    style = ttk.Style(root)
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass  # clam is shipped with every std tk; belt-and-braces only

    # The font family is RESOLVED, never '' (see _FONT_STACKS): on a
    # Chinese-locale Windows '' fell through to SimSun and the whole
    # surface rendered in serifs. '' only when no stack family exists.
    try:
        from tkinter import font as tkfont  # noqa: PLC0415 — lazy
        _family = pick_font_family(set(tkfont.families(root)))
    except Exception:
        _family = ''

    def _f(size, weight='normal'):
        return (_family, size, weight) if _family else ('', size, weight)

    base_font = _f(10)
    style.configure('Tofu.TFrame', background=p['bg'])
    style.configure('Card.TFrame', background=p['bg2'])
    style.configure('Tofu.TLabel', background=p['bg'], foreground=p['text'],
                    font=base_font)
    style.configure('Tofu.Title.TLabel', background=p['bg'],
                    foreground=p['text'], font=_f(15, 'bold'))
    style.configure('Tofu.Sub.TLabel', background=p['bg'],
                    foreground=p['text2'], font=_f(9))
    style.configure('Tofu.Err.TLabel', background=p['bg'],
                    foreground=p['error'], font=_f(9))
    style.configure('Card.TLabel', background=p['bg2'], foreground=p['text'],
                    font=base_font)
    style.configure('CardName.TLabel', background=p['bg2'],
                    foreground=p['text'], font=_f(10, 'bold'))
    style.configure('CardSub.TLabel', background=p['bg2'],
                    foreground=p['text2'], font=_f(9))
    # Section eyebrow inside a card (the「SERVER / 服务器」label row).
    style.configure('CardHead.TLabel', background=p['bg2'],
                    foreground=p['text3'], font=_f(8, 'bold'))
    style.configure('Status.Ok.TLabel', background=p['bg2'],
                    foreground=p['success'], font=_f(9))
    style.configure('Status.Err.TLabel', background=p['bg2'],
                    foreground=p['error'], font=_f(9))
    style.configure('Tofu.TCheckbutton', background=p['bg2'],
                    foreground=p['text'], font=base_font)
    style.map('Tofu.TCheckbutton',
              background=[('active', p['bg2'])],
              foreground=[('disabled', p['text3'])])
    # A checkbutton row with breathing room (tier rows in the role window).
    style.configure('Tier.TCheckbutton', background=p['bg2'],
                    foreground=p['text'], font=base_font, padding=(2, 2))
    style.map('Tier.TCheckbutton',
              background=[('active', p['bg2'])],
              foreground=[('disabled', p['text3'])])
    # A checkbutton sitting directly on the window background (the bottom
    # bar) — the card-flavoured one would paint a bg2 patch behind itself.
    style.configure('Bg.TCheckbutton', background=p['bg'],
                    foreground=p['text'], font=base_font)
    style.map('Bg.TCheckbutton',
              background=[('active', p['bg'])],
              foreground=[('disabled', p['text3'])])
    style.configure('Tofu.TButton', padding=(14, 7), background=p['bg3'],
                    foreground=p['text'], borderwidth=1, font=base_font)
    style.map('Tofu.TButton',
              background=[('active', p['hover']), ('disabled', p['bg3'])],
              foreground=[('disabled', p['text3'])])
    style.configure('Tofu.Accent.TButton', padding=(14, 7),
                    background=p['accent'], foreground=p['accent_fg'],
                    borderwidth=0, font=_f(10, 'bold'))
    style.map('Tofu.Accent.TButton',
              background=[('active', p['accent_hover']),
                          ('disabled', p['bg3'])],
              foreground=[('disabled', p['text3'])])
    style.configure('Tofu.Horizontal.TProgressbar', background=p['accent'],
                    troughcolor=p['bg3'], bordercolor=p['border'],
                    lightcolor=p['accent'], darkcolor=p['accent'])
    style.configure('Tofu.TEntry', fieldbackground=p['bg3'],
                    foreground=p['text'], insertcolor=p['text'],
                    bordercolor=p['border'])
    return p


def load_logo_photo(root, size=56):
    """Load the brand logo as a tk PhotoImage resized to ``size`` px.

    Returns None (never raises) when PIL or the asset is unavailable — a
    dialog without a logo is still a dialog. The image is anchored on the
    root so the garbage collector cannot reclaim it mid-display (the classic
    tk blank-image bug).
    """
    try:
        from PIL import Image, ImageTk  # noqa: PLC0415 — lazy
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if getattr(sys, 'frozen', False):
            bundle = os.path.join(os.path.dirname(sys.executable), '_internal')
            if os.path.isdir(bundle):
                base = bundle
        path = os.path.join(base, 'static', 'icons', 'logo.png')
        if not os.path.isfile(path):
            return None
        img = Image.open(path).convert('RGBA').resize((size, size),
                                                      Image.LANCZOS)
        photo = ImageTk.PhotoImage(img, master=root)
        # Anchor EVERY photo on the root — a single-slot anchor meant the
        # 64px iconphoto was GC'd the moment the 40px header logo loaded
        # (the classic tk blank-image bug, one window over).
        photos = getattr(root, '_tofu_logo_photos', None)
        if photos is None:
            photos = root._tofu_logo_photos = []
        photos.append(photo)
        return photo
    except Exception:
        return None


def card_frame(parent, palette):
    """A bordered 'card' container matching the web UI's component cards.

    tk.Frame (not ttk) because only tk frames take a real border color via
    highlightthickness/highlightbackground — ttk frames ignore both.
    """
    import tkinter as tk  # noqa: PLC0415 — lazy
    return tk.Frame(parent, bg=palette['bg2'],
                    highlightthickness=1,
                    highlightbackground=palette['border'],
                    highlightcolor=palette['border'])
