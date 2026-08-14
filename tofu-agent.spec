# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for TofuAgent — the controlled-machine component.

Build:
    pyinstaller tofu-agent.spec

Output:
    dist/TofuAgent/              — self-contained app directory
    dist/TofuAgent/TofuAgent.exe — main executable

The mirror image of tofu.spec, minus the ENTIRE server stack: this bundle
carries the desktop-agent closure only (poll loop, dispatch table, connect
dialog, tray). No Quart/Hypercorn/Flask, no DB, no frontend, no playwright —
the frozen size claim is enforced by the excludes list below AND by the
TOFU_AGENT_SMOKE gate in desktop/agent_launcher.py, which asserts none of
the server-stack modules can even be imported in the frozen build.
"""

import os
import shutil
import sys
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Same UPX policy as tofu.spec: only when installed, fragile DLLs excluded.
_UPX_AVAILABLE = shutil.which('upx') is not None
_UPX_EXCLUDE = [
    'vcruntime140.dll', 'vcruntime140_1.dll', 'python3.dll',
    'python312.dll', 'python311.dll', 'msvcp140.dll',
]

ROOT = os.path.abspath(SPECPATH)

# ── Hidden imports: the agent closure, nothing more ──
hidden_imports = []

# The agent package itself (dispatch table built by package __init__).
hidden_imports += collect_submodules('lib.desktop_agent')

# The agent's lib dependencies (stdlib-only, but PyInstaller's static
# analysis follows them anyway — listed for explicitness).
hidden_imports += ['lib.log', 'lib.json_store', 'lib.version',
                   'lib.runtime_paths']

# The desktop package seams the launcher uses.
hidden_imports += ['desktop.connect_ui', 'desktop._tk_theme',
                   'desktop.role_window', 'desktop._tk_host']

# Third-party agent deps.
hidden_imports += [
    'requests',
    'pystray',
    'PIL', 'PIL.Image',
    'psutil',
    # pyautogui's platform backends are imported dynamically.
    'pyautogui', 'pyperclip', 'pygetwindow', 'pyscreeze', 'pytweening',
    'mouseinfo',
    # The connect dialog's toolkit (the smoke gate hard-asserts this).
    'tkinter', 'tkinter.ttk',
]

# curl_cffi is the egress epic's planned OPTIONAL TLS-fingerprint dep
# (docs/DESKTOP_EGRESS_DESIGN.md §5 note). Bundle it when the build
# environment has it, so the packaging needs no change the day it lands.
try:
    import curl_cffi  # noqa: F401
    hidden_imports += collect_submodules('curl_cffi')
except ImportError:
    pass

# ── Data files: ONLY what the tray + dialog + installer need ──
# No static/ frontend, no index.html, no browser_extension — those are
# the full app's payload. VERSION feeds _agent_version (the drift frame).
_candidate_datas = [
    (os.path.join(ROOT, 'static', 'icons'), 'static/icons'),
    (os.path.join(ROOT, 'VERSION'), '.'),
]

datas = []
for _src, _dst in _candidate_datas:
    if os.path.exists(_src):
        datas.append((_src, _dst))
    else:
        print('[tofu-agent.spec] SKIP missing data file: %s' % _src)

# ── Exclusions: the size claim, enforced ──
# Everything the FULL app bundles that the agent must not. The smoke gate
# re-asserts this at runtime in the frozen build; the excludes make the
# failure cheap (a smaller bundle) rather than only detectable.
excludes = [
    # Test/dev
    'matplotlib', 'numpy', 'scipy', 'pandas',
    'IPython', 'notebook', 'jupyter',
    'torch', 'tensorflow', 'transformers',
    'pytest', 'ruff',
    'onnxruntime', 'onnx',
    # ── The server stack (this is the point of the component) ──
    'server',
    'quart', 'jinja2',
    'hypercorn', 'cryptography',
    'psycopg2', 'sqlite3',
    'playwright',
    'trafilatura', 'lxml', 'bs4', 'lxml_html_clean',
    'fitz', 'pymupdf',
    'docx', 'openpyxl', 'xlrd', 'olefile',
    'markdown',
    'mcp',
]

a = Analysis(
    [os.path.join(ROOT, 'desktop', 'agent_launcher.py')],
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

a.datas = [d for d in a.datas if not d[0].startswith('tests/')]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

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
    name='TofuAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=_UPX_AVAILABLE,
    console=False,          # No terminal window — diagnostics go to the log file
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
    upx=_UPX_AVAILABLE,
    upx_exclude=_UPX_EXCLUDE,
    name='TofuAgent',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='TofuAgent.app',
        icon=_icon_mac if os.path.isfile(_icon_mac) else None,
        bundle_identifier='com.tofu.desktop-agent',
        info_plist={
            'CFBundleDisplayName': 'Tofu Agent',
            'CFBundleShortVersionString': open(
                os.path.join(ROOT, 'VERSION')).read().strip(),
            'NSHighResolutionCapable': True,
            'LSBackgroundOnly': False,
        },
    )
