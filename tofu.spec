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
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

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
]

# ── Data files to bundle ──
datas = [
    # Frontend
    (os.path.join(ROOT, 'static'), 'static'),
    (os.path.join(ROOT, 'index.html'), '.'),
    (os.path.join(ROOT, 'trading.html'), '.'),

    # Version file
    (os.path.join(ROOT, 'VERSION'), '.'),

    # .env.example as template
    (os.path.join(ROOT, '.env.example'), '.'),

    # Provider templates (loaded at runtime by lib/llm_dispatch)
    # Bundled inside static/ already — no extra entry needed
]

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
    upx=True,
    console=False,          # No terminal window on Windows/macOS
    disable_windowed_traceback=False,
    argv_emulation=False,
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
    upx=True,
    upx_exclude=[],
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
