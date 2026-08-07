"""debug/win_ci_shot.py — drive the custom NSIS wizard on a headless Windows
host and capture per-page PIXELS + the TOFU_DIAG fact log.

Why this exists (2026-08-07): the blank-wizard hunt needed pixel-level
runtime verification, but the build box's wine cannot run 32-bit apps
(SIGSYS) and every human test cycle costs a day. A GitHub Actions
windows-latest runner IS a real Windows: this script builds the installer
there (stub payload — the wizard's pages are identical regardless of
payload content) and drives it end to end, no human in the loop.

Capture method: PrintWindow(PW_RENDERFULLCONTENT) — the window renders
ITSELF into our DC, so it works in session 0 where desktop screenshots
come back black. Fallback chain: PrintWindow(2) → PrintWindow(0) →
BitBlt from the window DC. Uniform-black frames are detected and the
fallback engages automatically.

Page turns: the diag build writes `$TEMP\\tofu-setup-diag.log` with a
"<page>: reached Show" marker per page; we poll that log and shoot each
page as it appears (the progress page auto-advances when the install
body finishes — a blind click cadence would miss it). Next/Finish are
clicked via BM_CLICK to control ID 1. If the log never appears (e.g.
--no-diag build), a blind settle-and-click fallback still produces
page1..page4 frames.

Usage (Windows only for the drive; --build-only works anywhere):

    python debug/win_ci_shot.py --out probe-out --target agent
    python debug/win_ci_shot.py --drive-only path\\to\\setup.exe

Artifacts in --out: welcome.png / directory.png / progress.png /
finish.png (as captured), buttons.json (nav-button text readback per
page), summary.json (window facts + per-page capture method + pixel
extrema), tofu-setup-diag.log (when the diag seam is compiled in).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types

PAGES = ('welcome', 'directory', 'progress', 'finish')
BM_CLICK = 0x00F5
PW_RENDERFULLCONTENT = 2
DIB_RGB_COLORS = 0
LOG_NAME = 'tofu-setup-diag.log'

if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [('biSize', wintypes.DWORD),
                    ('biWidth', wintypes.LONG),
                    ('biHeight', wintypes.LONG),
                    ('biPlanes', wintypes.WORD),
                    ('biBitCount', wintypes.WORD),
                    ('biCompression', wintypes.DWORD),
                    ('biSizeImage', wintypes.DWORD),
                    ('biXPelsPerMeter', wintypes.LONG),
                    ('biYPelsPerMeter', wintypes.LONG),
                    ('biClrUsed', wintypes.DWORD),
                    ('biClrImportant', wintypes.DWORD)]


# ── Building the diag installer (platform-independent) ──────────────────────

def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_installer(target: str, makensis: str, workdir: str, *,
                    diag: bool = True) -> str:
    """Stub payload + real template + wrap-time art → NSIS compile.

    The wizard UI is payload-independent (pages are baked bitmaps + live
    controls), so a stub payload measures it faithfully. The stub exe is
    a copy of whoami.exe: the finish page's launch-after-install Execs
    it, and a real (harmless, instantly-exiting) exe keeps that lane
    error-free on the runner.
    """
    sys.path.insert(0, _repo_root())
    # Namespace-injection bypass: lib/__init__.py is the server's fat
    # facade — it eagerly pulls flask/redis/psycopg2/… via lib.pricing,
    # which a bare CI runner does not have (first probe run died at
    # 'No module named requests', 2026-08-07). The render chain itself
    # needs only stdlib + PIL, so register minimal parent packages with
    # __path__ and the submodule imports below never execute the
    # facade. Local proof: render succeeds with requests/flask/redis/
    # psycopg2/cryptography/matplotlib all import-blocked.
    root = _repo_root()
    if 'lib' not in sys.modules:
        pkg = types.ModuleType('lib')
        pkg.__path__ = [os.path.join(root, 'lib')]
        sys.modules['lib'] = pkg
    if 'lib.desktop_dist' not in sys.modules:
        pkg = types.ModuleType('lib.desktop_dist')
        pkg.__path__ = [os.path.join(root, 'lib', 'desktop_dist')]
        sys.modules['lib.desktop_dist'] = pkg
    from lib.desktop_dist import installer_art, winbuilder as wb

    nt = wb._NSI_TARGETS[target]
    payload = os.path.join(workdir, 'payload')
    os.makedirs(os.path.join(payload, '_internal'), exist_ok=True)
    whoami = os.path.join(os.environ.get('WINDIR', r'C:\Windows'),
                          'System32', 'whoami.exe')
    shutil.copy2(whoami, os.path.join(payload, nt['app_exe']))
    with open(os.path.join(payload, '_internal', 'stub.txt'), 'w') as f:
        f.write('stub payload — wizard UI probe only\n')

    art_dir = os.path.join(workdir, 'art')
    installer_art.render(art_dir, nt['app_name'], '0.0.0-ci',
                         autostart=bool(nt['autostart_value']))

    out_file = os.path.join(workdir, f'probe-{target}.exe')
    nsi = wb._render_nsi('0.0.0-ci', payload, out_file, target,
                         art_dir=art_dir)
    script = os.path.join(workdir, 'installer.nsi')
    with open(script, 'w', encoding='utf-8') as f:
        f.write(nsi)
    cmd = [makensis, '-V2']
    if diag:
        cmd.append('-DTOFU_DIAG=1')
    cmd.append(script)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if p.returncode != 0 or not os.path.isfile(out_file):
        raise RuntimeError(f'makensis failed:\n{p.stdout[-3000:]}')
    return out_file


def _find_makensis() -> str:
    candidates = [
        os.environ.get('MAKENSIS', ''),
        r'C:\Program Files (x86)\NSIS\makensis.exe',
        r'C:\Program Files\NSIS\makensis.exe',
        shutil.which('makensis') or '',
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise RuntimeError('makensis not found — pass --makensis or '
                       'choco install nsis')


# ── Windows driving (win32 only) ────────────────────────────────────────────

def _require_windows():
    if sys.platform != 'win32':
        raise RuntimeError('win_ci_shot drive mode is Windows-only '
                           '(the whole point is a REAL Windows GUI)')


def _window_text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_wizard(title_sub: str):
    """The top-level wizard window: title contains the substring AND the
    window owns a Next button (control ID 1) — title-only matching would
    also catch an Explorer window browsing a same-named folder."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            t = _window_text(hwnd)
            if title_sub.lower() in t.lower() and \
                    user32.GetDlgItem(hwnd, 1):
                found.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def wait_for_wizard(title_sub: str, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = find_wizard(title_sub)
        if hwnd:
            return hwnd
        time.sleep(0.25)
    raise RuntimeError(f'wizard window "{title_sub}" never appeared '
                       f'({timeout}s)')


def control_texts(hwnd, ids=(1, 2, 3)) -> dict:
    """Read back the nav buttons' ACTUAL text (1=Next, 2=Cancel,
    3=Back) — the 2026-08-07 real-machine screenshot showed the stock
    text where the wizard retitles it, so the retitle lane is measured,
    never assumed."""
    out = {}
    for cid in ids:
        h = user32.GetDlgItem(hwnd, cid)
        out[str(cid)] = _window_text(h) if h else None
    return out


def _shot_once(hwnd, method: str) -> 'object':
    from PIL import Image

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError(f'degenerate window rect {w}x{h}')
    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    gdi32.SelectObject(mem_dc, bmp)
    try:
        if method == 'printwindow-full':
            user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
        elif method == 'printwindow-legacy':
            user32.PrintWindow(hwnd, mem_dc, 0)
        else:  # bitblt
            gdi32.BitBlt(mem_dc, 0, 0, w, h, hwnd_dc, 0, 0, 0x00CC0020)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h  # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf,
                              ctypes.byref(bmi), DIB_RGB_COLORS)
        if got != h:
            raise RuntimeError(f'GetDIBits returned {got}/{h}')
        return Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def shot(hwnd, out_png: str) -> dict:
    """One frame with the black-frame fallback chain. Returns the capture
    record for the summary (method used + pixel extrema — the extrema
    are what make 'all-black frame' a measured fact, not a guess)."""
    last = None
    for method in ('printwindow-full', 'printwindow-legacy', 'bitblt'):
        try:
            img = _shot_once(hwnd, method)
        except Exception as e:  # noqa: BLE001 — try the next method
            last = e
            continue
        extrema = img.convert('L').getextrema()
        rec = {'file': os.path.basename(out_png), 'method': method,
               'lum_extrema': list(extrema), 'size': list(img.size)}
        if extrema == (0, 0) and method != 'bitblt':
            continue  # all black — the next method may do better
        img.convert('RGB').save(out_png)
        return rec
    raise RuntimeError(f'every capture method failed (last: {last})')


def click_next(hwnd) -> None:
    btn = user32.GetDlgItem(hwnd, 1)
    if not btn:
        raise RuntimeError('Next button (ID 1) not found')
    user32.SendMessageW(btn, BM_CLICK, 0, 0)


def _log_pages(log_path: str) -> list:
    """The "<page>: reached Show" markers written so far, in order."""
    if not os.path.isfile(log_path):
        return []
    with open(log_path, encoding='utf-8', errors='replace') as f:
        body = f.read()
    return [p for p in PAGES if f'{p}: reached Show' in body]


def drive(exe: str, outdir: str, *, title: str, settle: float,
          timeout: float) -> dict:
    _require_windows()
    diag_log = os.path.join(tempfile.gettempdir(), LOG_NAME)
    if os.path.isfile(diag_log):
        os.unlink(diag_log)  # a stale log would fake page detection
    proc = subprocess.Popen([exe])
    summary = {'exe': exe, 'pid': proc.pid, 'pages': {},
               'buttons': {}, 'log_copied': False}
    seen = set()
    try:
        hwnd = wait_for_wizard(title, timeout)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        summary['window'] = {
            'hwnd': hwnd, 'visible': bool(user32.IsWindowVisible(hwnd)),
            'rect': [rect.left, rect.top, rect.right, rect.bottom],
        }
        dc = user32.GetDC(0)
        summary['runner_dpi'] = gdi32.GetDeviceCaps(dc, 90)
        user32.ReleaseDC(0, dc)

        deadline = time.time() + timeout
        while time.time() < deadline:
            pages = _log_pages(diag_log)
            new = [p for p in pages if p not in seen]
            if new:
                page = new[-1]
                time.sleep(settle)  # let the page finish painting
                hwnd = find_wizard(title) or hwnd
                summary['pages'][page] = shot(
                    hwnd, os.path.join(outdir, f'{page}.png'))
                summary['buttons'][page] = control_texts(hwnd)
                seen.add(page)
                if page in ('welcome', 'directory'):
                    click_next(hwnd)
                elif page == 'finish':
                    click_next(hwnd)  # Finish closes the wizard
                    break
            elif proc.poll() is not None and not pages:
                # The wizard died before any marker — capture what is
                # (or is no longer) there and bail.
                break
            time.sleep(0.2)

        # The blind fallback: no diag log (non-diag build) — settle and
        # click through all four pages by number.
        if not seen:
            hwnd = wait_for_wizard(title, 30)
            for i, page in enumerate(PAGES):
                summary['pages'][page] = shot(
                    hwnd, os.path.join(outdir, f'{page}.png'))
                summary['buttons'][page] = control_texts(hwnd)
                if i < len(PAGES) - 1:
                    click_next(hwnd)
                    time.sleep(max(settle, 3.0))
                else:
                    click_next(hwnd)
        time.sleep(2)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if os.path.isfile(diag_log):
            shutil.copy2(diag_log, os.path.join(outdir, LOG_NAME))
            summary['log_copied'] = True
        with open(os.path.join(outdir, 'buttons.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(summary['buttons'], f, ensure_ascii=False, indent=2)
        with open(os.path.join(outdir, 'summary.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2,
                      default=str)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='NSIS wizard pixel probe')
    ap.add_argument('--out', default='probe-out')
    ap.add_argument('--target', default='agent', choices=('agent', 'full'))
    ap.add_argument('--makensis', default='')
    ap.add_argument('--title', default='',
                    help='wizard title substring (default: the target '
                         'app name)')
    ap.add_argument('--settle', type=float, default=0.3,
                    help='paint settle seconds before each shot')
    ap.add_argument('--timeout', type=float, default=240)
    ap.add_argument('--build-only', action='store_true')
    ap.add_argument('--no-diag', action='store_true')
    ap.add_argument('--drive-only', default='',
                    help='skip the build; drive this existing exe')
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    exe = args.drive_only
    if not exe:
        workdir = os.path.join(tempfile.gettempdir(),
                               f'win-ci-shot-{int(time.time())}')
        os.makedirs(workdir, exist_ok=True)
        exe = build_installer(args.target,
                              args.makensis or _find_makensis(),
                              workdir, diag=not args.no_diag)
        shutil.copy2(exe, os.path.join(args.out, os.path.basename(exe)))
        print(f'[win-ci-shot] built {exe}')
    if args.build_only:
        return 0
    title = args.title or ('Tofu Agent' if args.target == 'agent'
                           else 'Tofu')
    summary = drive(exe, args.out, title=title, settle=args.settle,
                    timeout=args.timeout)
    got = set(summary['pages'])
    missing = [p for p in ('welcome', 'finish') if p not in got]
    print(f'[win-ci-shot] pages captured: {sorted(got)}; '
          f'log={summary["log_copied"]}')
    if missing:
        print(f'[win-ci-shot] MISSING critical pages: {missing}',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'[win-ci-shot] FAILED: {e}', file=sys.stderr)
        sys.exit(2)
