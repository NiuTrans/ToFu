# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for Tofu (豆腐) desktop application.

Build:
    pyinstaller tofu.spec

Output:
    dist/Tofu/          — self-contained app directory
    dist/Tofu/Tofu      — main executable (Tofu.exe on Windows)

The spec uses --onedir mode for faster startup and easier debugging.
An installer (Inno Setup / create-dmg) wraps this into a single .exe/.dmg.
"""

import os
import shutil
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# ── UPX: only enable when the compressor is actually installed ──
# UPX shrinks the bundle, but the CI runners do NOT install it. With upx=True
# and no upx binary PyInstaller merely warns and skips — harmless. The real
# hazard is a HALF-configured runner where UPX exists but mangles native
# Windows DLLs (VCRUNTIME140.dll, python3xx.dll, Qt), producing an exe that
# crashes only on a clean machine. So we gate on presence AND keep the known
# fragile DLLs out of compression.
_UPX_AVAILABLE = shutil.which('upx') is not None
_UPX_EXCLUDE = [
    'vcruntime140.dll', 'vcruntime140_1.dll', 'python3.dll',
    'python312.dll', 'python311.dll', 'msvcp140.dll',
]

# ── Project root ──
# SPECPATH is the directory containing this spec file, which IS the project
# root (tofu.spec lives at the repo top level). Do NOT take dirname() — that
# would point one level ABOVE the repo and break all ROOT-relative paths
# (e.g. desktop/launcher.py) during CI builds.
ROOT = os.path.abspath(SPECPATH)

# ── Hidden imports: dynamically-imported modules that PyInstaller misses ──
hidden_imports = []

# The server entry module — imported dynamically via runpy.run_module('server')
# in the desktop launcher's frozen self-relaunch path, so static analysis
# misses it. Must be listed explicitly or the frozen build can't start.
hidden_imports += ['server']

# All lib subpackages (Flask blueprints, LLM dispatch, tools, etc.)
hidden_imports += collect_submodules('lib')
hidden_imports += collect_submodules('routes')

# Hypercorn ASGI server (the desktop build runs the real Hypercorn startup).
hidden_imports += ['hypercorn', 'hypercorn.asyncio', 'hypercorn.config',
                   'hypercorn.protocol', 'cryptography']

# Flask internals
hidden_imports += ['flask.json', 'flask.templating', 'jinja2.ext']

# Database backends (PG primary, SQLite fallback)
hidden_imports += ['sqlite3', 'json', 'psycopg2', 'psycopg2.extensions',
                   'psycopg2.extras', 'psycopg2._psycopg']

# Optional but bundled
hidden_imports += [
    'PIL', 'PIL.Image', 'PIL.ImageDraw',
    'pystray',
    'trafilatura',
    'lxml', 'lxml.html', 'lxml.etree', 'lxml_html_clean',
    'bs4', 'dateutil', 'dateutil.parser',
    'docx', 'openpyxl', 'xlrd', 'olefile',
    'fitz', 'pymupdf',
    'markdown',
    'psutil',
    'flask_compress',
    'mcp',
    'playwright', 'playwright.sync_api', 'playwright.async_api',
    'tkinter', 'tkinter.ttk',
    # Desktop-control agent (lib/desktop_agent): local machine automation.
    # pyautogui pulls in platform-specific backends dynamically.
    'pyautogui', 'pyperclip', 'pygetwindow', 'pyscreeze', 'pytweening', 'mouseinfo',
]

# ── Data files to bundle ──
# Each entry is (source, dest). Some sources are OPTIONAL — a missing source in
# `datas` is a HARD ERROR that aborts the whole build (PyInstaller raises
# "Unable to find file …"). ``trading.html`` in particular was moved to the
# external tofu-trading plugin and no longer exists in-tree, so listing it
# unconditionally broke every build. We therefore filter to existing paths and
# log what we drop, instead of crashing.
_candidate_datas = [
    # Frontend
    (os.path.join(ROOT, 'static'), 'static'),
    (os.path.join(ROOT, 'index.html'), '.'),
    (os.path.join(ROOT, 'trading.html'), '.'),   # optional (legacy plugin shell)

    # Version file
    (os.path.join(ROOT, 'VERSION'), '.'),

    # .env.example as template
    (os.path.join(ROOT, '.env.example'), '.'),

    # Browser-extension source, served as a ZIP by /api/browser/download and
    # surfaced as `extensionPath` by /api/v1/browser/status.
    #
    # Both read it from disk at REQUEST time — routes/browser.py builds the zip
    # by walking ``BASE_DIR/browser_extension`` and 404s when that directory is
    # absent. Without this entry the frozen app ships no such directory, so the
    # Local Control modal's "Download extension ZIP" button 404s and
    # extensionPath is permanently null: the desktop build is exactly the one
    # where the user cannot obtain the extension, which is the population that
    # most needs it. The dest is bare ``browser_extension`` (not ``.``) so it
    # lands at ``_internal/browser_extension`` — the same directory those two
    # modules derive from their own ``__file__`` when frozen.
    (os.path.join(ROOT, 'browser_extension'), 'browser_extension'),

    # Provider templates (loaded at runtime by lib/llm_dispatch)
    # Bundled inside static/ already — no extra entry needed
]

datas = []
for _src, _dst in _candidate_datas:
    if os.path.exists(_src):
        datas.append((_src, _dst))
    else:
        print('[tofu.spec] SKIP missing data file: %s' % _src)

# Collect data files from packages that ship non-Python assets
datas += collect_data_files('trafilatura', include_py_files=False)
datas += collect_data_files('flask', include_py_files=False)
datas += collect_data_files('flask_compress', include_py_files=False)
datas += collect_data_files('jinja2', include_py_files=False)
datas += collect_data_files('certifi', include_py_files=False)

# ── Exclusions: reduce bundle size ──
excludes = [
    'matplotlib', 'numpy', 'scipy', 'pandas',
    'IPython', 'notebook', 'jupyter',
    'torch', 'tensorflow', 'transformers',
    'pytest', 'ruff',
    # NOTE: playwright Python package IS bundled (~5MB) so that
    # `python -m playwright install chromium` works post-install.
    # The Chromium browser binary (~150MB) is NOT bundled — it's
    # offered as an optional download on first launch.
]

# ── Analysis ──
a = Analysis(
    [os.path.join(ROOT, 'desktop', 'launcher.py')],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ── Remove unnecessary binaries to shrink the bundle ──
# Filter out test files and docs accidentally pulled in
a.datas = [d for d in a.datas if not d[0].startswith('tests/')]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ── Platform-specific icon ──
_icon_win = os.path.join(ROOT, 'static', 'icons', 'tofu.ico')
_icon_mac = os.path.join(ROOT, 'static', 'icons', 'tofu.icns')

icon_file = None
if sys.platform == 'win32' and os.path.isfile(_icon_win):
    icon_file = _icon_win
elif sys.platform == 'darwin' and os.path.isfile(_icon_mac):
    icon_file = _icon_mac

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # --onedir mode
    name='Tofu',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=_UPX_AVAILABLE,
    console=False,          # No terminal window on Windows/macOS
    disable_windowed_traceback=False,
    argv_emulation=False,
    # Native host arch (None). The CI macOS matrix builds arm64 on macos-14 and
    # x86_64 on macos-13 separately, each with arch-matching wheels. Do NOT set
    # 'universal2' here: the runners' interpreter + several bundled C extensions
    # (psycopg2, pymupdf, lxml) ship single-arch wheels, so a universal2 COLLECT
    # aborts with "not a fat binary!" (see .github/workflows/build-desktop.yml).
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=_UPX_AVAILABLE,
    upx_exclude=_UPX_EXCLUDE,
    name='Tofu',
)

# ── macOS .app bundle ──
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='Tofu.app',
        icon=_icon_mac if os.path.isfile(_icon_mac) else None,
        bundle_identifier='com.tofu.desktop',
        info_plist={
            'CFBundleDisplayName': 'Tofu',
            'CFBundleShortVersionString': open(
                os.path.join(ROOT, 'VERSION')).read().strip(),
            'NSHighResolutionCapable': True,
            'LSBackgroundOnly': False,
        },
    )
