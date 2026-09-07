"""tests/test_installer_parity.py — the installer authorings must agree.

WHY THIS EXISTS
---------------
The CI builds the Windows installer with Inno Setup (a heredoc inside
.github/workflows/build-desktop.yml); the server-side build uses NSIS
(desktop/installer.nsi.tmpl), because every 32-bit Windows app measured
hangs under the server box's preloader-less WoW64 and Inno 7's container
resists extraction (2026-08-01, see the .nsi header).

Two authorings of one installer will drift — and since 2026-08-02 there
are also TWO COMPONENTS (full app / agent, docs/DESKTOP_AGENT_DIST_DESIGN
.md) rendered from ONE parametrized template. The defence is NOT
pretending there is one file — it is pinning the SEMANTIC CONTRACT they
must all honour, asserted on the RENDERED scripts (winbuilder._render_nsi),
not on the raw template: app name, install dir, privilege level, the two
shortcuts, launch-after-install, output naming, wizard branding, payload
shape, Add/Remove-Programs registration, and — for the agent — the boot-
autostart contract (owner amendment ①: default-ON, HKCU, removed at
uninstall, value name shared with the tray toggle).

2026-08-04 MODERNIZATION (owner: "the installer looks 2000s")
-------------------------------------------------------------
The NSIS side dropped MUI2 for a fully custom nsDialogs wizard (baked
page art + LangString labels on #F0F0F0 cards, marquee progress, /SOLID
lzma, ManifestDPIAware). The Inno side keeps the classic wizard — the CI
heredoc cannot be runtime-tested from this box, so the VISUAL divergence
is deliberate and documented here: the shared contract is SEMANTIC
(branding present, same install result), not pixel identity. The
ratchets for the new authoring live at the bottom of this file; the
makensis compile gate is tests/test_installer_nsi_compile.py; the
art↔template geometry contract is tests/test_installer_art.py.

Run:  pytest tests/test_installer_parity.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.desktop_dist import winbuilder as wb

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = (_ROOT / '.github' / 'workflows' / 'build-desktop.yml') \
    .read_text(encoding='utf-8')
_NSI = (_ROOT / 'desktop' / 'installer.nsi.tmpl') \
    .read_text(encoding='utf-8')

# The rendered scripts are the contract surface — placeholders are the
# mechanism, the rendering is what makensis compiles.
_FULL = wb._render_nsi('0.16.0', '/payload', '/out.exe', 'full',
                       art_dir='/art')
_AGENT = wb._render_nsi('0.16.0', '/payload', '/out.exe', 'agent',
                        art_dir='/art')


def _expand(script: str, target: str) -> str:
    """Resolve the NSIS ${APP_NAME}/${APP_EXE} defines, so assertions read
    what the INSTALLED result is (shortcut names, dirs) — not the
    indirection."""
    t = wb._NSI_TARGETS[target]
    return (script.replace('${APP_NAME}', t['app_name'])
                  .replace('${APP_EXE}', t['app_exe']))


def _code(script: str) -> str:
    """Drop NSIS comment lines — the template's own documentation names
    the machinery, and absence assertions must target CODE."""
    return '\n'.join(l for l in script.splitlines()
                     if not l.lstrip().startswith(';'))


# ═══════════════════════════════════════════════════════════════════
#  FULL component — must stay behavior-identical to the historical installer
# ═══════════════════════════════════════════════════════════════════

def test_app_name_and_install_dir_agree():
    assert 'AppName=Tofu\n' in _WORKFLOW, (
        'the full Inno authoring must name the app exactly "Tofu" — '
        '"AppName=Tofu Agent" satisfies a bare substring check, so this '
        'assertion is newline-anchored')
    assert '!define APP_NAME      "Tofu"' in _FULL
    # Inno {localappdata}\Programs\Tofu ≡ NSIS $LOCALAPPDATA\Programs\Tofu
    assert r'{localappdata}\\Programs\\Tofu' in _WORKFLOW
    assert r'$LOCALAPPDATA\Programs\Tofu' in _FULL


def test_privilege_level_agrees():
    """Both are per-user installs with no UAC — the whole reason the app's
    portable data dir is writable on first launch."""
    assert 'PrivilegesRequired=lowest' in _WORKFLOW
    assert 'RequestExecutionLevel user' in _NSI


def test_shortcuts_and_launch_after_install_agree():
    # Start-menu shortcut (the workflow heredoc carries literal double
    # backslashes — assert the text as it IS, not as Inno would parse it)
    assert r'{autoprograms}\\Tofu' in _WORKFLOW
    assert r'$SMPROGRAMS\Tofu' in _expand(_FULL, 'full')
    # Desktop shortcut
    assert r'{autodesktop}\\Tofu' in _WORKFLOW
    assert r'$DESKTOP\Tofu.lnk' in _expand(_FULL, 'full')
    # Launch after install: Inno Flags + the custom wizard's finish-page
    # checkbox (default checked) whose leave handler Execs the app — the
    # semantic equivalent of the old MUI_FINISHPAGE_RUN define.
    assert 'Flags: nowait postinstall' in _WORKFLOW
    assert '${NSD_Check} $ChkLaunch' in _FULL
    assert "Exec '\"$INSTDIR\\${APP_EXE}\"'" in _FULL
    # Uninstall path exists in both (Inno generates one; NSIS writes one)
    assert 'WriteUninstaller' in _FULL
    assert 'Function un.DoUninstall' in _FULL


def test_output_name_pattern_agrees():
    assert 'OutputBaseFilename=Tofu-Setup-${APP_VERSION}-win64' in _WORKFLOW
    # The server's wrapper names the file by the same formula, driven by
    # the per-target identity table (not a literal that can drift).
    assert wb._NSI_TARGETS['full']['setup_prefix'] == 'Tofu-Setup'
    import inspect
    src = inspect.getsource(wb.wrap_payload)
    assert 'setup_prefix' in src and '-win64.exe' in src, (
        'the wrapper renamed the installer — CI and server must produce '
        'the SAME filename shape or the artifact store cannot tell them '
        'apart by design')


def test_wizard_branding_on_both_sides():
    """One brand, two renderers — a rebrand must land in both. Inno keeps
    the classic sidebar bitmap; the custom NSIS wizard renders full-page
    art at wrap time (lib/desktop_dist/installer_art.py)."""
    # ── Inno side (CI): unchanged classic assets ──
    for asset in (r'static\\icons\\tofu.ico',
                  r'static\\icons\\installer\\wizard-large.bmp'):
        assert asset in _WORKFLOW, f'{asset} missing from the Inno authoring'
    # ── NSIS side: the app icon + the four wrap-time pages ──
    assert 'tofu.ico' in _NSI, 'the wizard lost the tofu app icon'
    for page in ('welcome', 'directory', 'progress', 'finish'):
        assert f'@ART_DIR@/{page}.bmp' in _NSI, (
            f'the template no longer ships the {page} page art')
    import inspect
    src = inspect.getsource(wb.wrap_payload)
    assert 'installer_art.render' in src, (
        'wrap_payload stopped rendering the wizard art — makensis would '
        'fail on missing bitmap files')


def test_payload_shape_agrees():
    """Both pack the PyInstaller output tree with the app exe at its root."""
    assert r'dist\\Tofu\\*' in _WORKFLOW
    # POSIX glob in the .nsi — the native linux makensis treats a
    # backslash as a literal, not a separator.
    assert '@PAYLOAD_DIR@/*' in _NSI
    # The preseed contract: the file rides INSIDE the payload, next to the
    # exe, and the launchers import it on first run. Three pieces that
    # must name the same file or the zero-paste flow silently degrades.
    assert 'preseed_server.json' in _NSI
    connect_ui = (_ROOT / 'desktop' / 'connect_ui.py') \
        .read_text(encoding='utf-8')
    assert 'preseed_server.json' in connect_ui, (
        'connect_ui lost the preseed import — the .nsi ships a file '
        'nothing reads')


def test_add_remove_programs_registration():
    """Inno registers ARP automatically; the NSIS authoring must write the
    key by hand (2026-08-04 gap closed — the classic template silently
    never registered, so "Apps & features" had no Tofu entry)."""
    key = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Tofu'
    expanded = _expand(_FULL, 'full')
    for value in ('DisplayName', 'DisplayVersion', 'Publisher',
                  'UninstallString', 'DisplayIcon'):
        assert f'"{value}"' in expanded and key in expanded, (
            f'ARP value {value} missing from the NSIS authoring')
    # The uninstaller deletes exactly that key.
    assert f'DeleteRegKey HKCU \\\n    "{key}"' in expanded or \
        f'DeleteRegKey HKCU "{key}"' in expanded


# ═══════════════════════════════════════════════════════════════════
#  AGENT component — identity + the autostart contract (owner amendment ①)
# ═══════════════════════════════════════════════════════════════════

def test_agent_rendering_has_its_own_identity():
    assert '!define APP_NAME      "Tofu Agent"' in _AGENT
    assert '!define APP_EXE       "TofuAgent.exe"' in _AGENT
    assert r'$LOCALAPPDATA\Programs\TofuAgent' in _AGENT
    assert wb._NSI_TARGETS['agent']['setup_prefix'] == 'TofuAgent-Setup'
    # Per-user, same as full — the autostart key is HKCU precisely because
    # the privilege model matches.
    assert 'RequestExecutionLevel user' in _AGENT


def test_full_rendering_has_no_autostart():
    """A user-present tray app must NOT grow a Run key (owner: agent only)."""
    code = _code(_FULL)
    # The LangString DEFINITIONS ship in every rendering (unused strings
    # are inert) — what must be absent is every USE of the machinery.
    assert 'ChkAutoStart' not in code
    run_key = r'Software\Microsoft\Windows\CurrentVersion\Run'
    assert run_key not in code, (
        'the full installer must not write a boot-autostart Run value')


def test_agent_autostart_default_on_uac_free_and_uninstalled():
    """The old default-ON section's contract, carried by the custom wizard:
    a default-CHECKED checkbox on the directory page (UI path) and a
    silent-mode default of ON (the checkbox never exists then — an empty
    handle means "not asked" = ON, so /S keeps the old semantics)."""
    # UI path: checkbox exists and is default-checked.
    assert '${NSD_CreateCheckBox} 20u 113u 226u 10u ' \
        '"$(TOFU_CHK_AUTOSTART)"' in _AGENT
    assert '${NSD_Check} $ChkAutoStart' in _AGENT
    # Silent path: empty handle → treated as checked.
    assert '${If} $ChkAutoStart == ""' in _AGENT
    # HKCU Run value (UAC-free), quoted-exe data, matching the tray's form.
    run_key = r'Software\Microsoft\Windows\CurrentVersion\Run'
    assert f'WriteRegStr HKCU "{run_key}" "TofuAgent"' in _AGENT
    assert '\'"$INSTDIR\\${APP_EXE}"\'' in _AGENT
    # The uninstaller removes it unconditionally — a removed app must not
    # leave a dead autorun pointing at a missing exe.
    assert f'DeleteRegValue HKCU "{run_key}" "TofuAgent"' in _AGENT


def test_the_autostart_value_name_is_shared_with_the_tray():
    """The installer, the tray toggle and the reconcile logic must write
    the SAME Run value name — three writers, one key, or the states
    diverge silently (installer says on, tray says off)."""
    import desktop.agent_launcher as al
    assert al._RUN_VALUE == 'TofuAgent'
    assert f'"{al._RUN_VALUE}"' in _AGENT


# ═══════════════════════════════════════════════════════════════════
#  The renderer itself — no placeholder may survive in either component
# ═══════════════════════════════════════════════════════════════════

def test_every_placeholder_is_substituted_in_both_renderings():
    for p in wb._NSI_PLACEHOLDERS:
        assert p not in _FULL, f'{p} left unrendered in the full script'
        assert p not in _AGENT, f'{p} left unrendered in the agent script'


def test_no_at_tokens_survive_anywhere():
    """The renderer is a GLOBAL string replace: an @-delimited token named
    in a comment expands there too (the 2026-08-02 makensis abort). After
    rendering, no '@' may remain in CODE (comments are prose and keep
    their explanatory @-mentions by design)."""
    assert '@' not in _code(_FULL)
    assert '@' not in _code(_AGENT)


# ═══════════════════════════════════════════════════════════════════
#  Running-app guard (owner report 2026-08-04: install died on a locked exe)
# ═══════════════════════════════════════════════════════════════════

def test_running_app_is_closed_gracefully_in_both_authorings():
    """Upgrading over a RUNNING app must prompt + auto-close, never die on
    a raw file-in-use error. NSIS: the TOFU_CLOSE_RUNNING_APP macro probes
    the exe image lock (append-open is refused on a running image) and
    kills the tree via nsExec (hidden console — taskkill must not flash
    its own black window). Inno: CloseApplications, pinned explicitly
    because it used to ride an unasserted default."""
    # ── NSIS side, both components ──
    assert '!macro TOFU_CLOSE_RUNNING_APP' in _NSI
    for script, target, exe in ((_FULL, 'full', 'Tofu.exe'),
                                (_AGENT, 'agent', 'TofuAgent.exe')):
        expanded = _expand(script, target)
        # Three insertion points: .onInit (default dir), the directory-
        # page leave (user retargeted another existing install), and
        # un.onInit (uninstall-while-running).
        assert expanded.count('!insertmacro TOFU_CLOSE_RUNNING_APP') == 3, (
            'the guard must run in .onInit, DirPageLeave and un.onInit')
        assert 'Function .onInit' in script
        assert 'Function DirPageLeave' in script
        assert 'Function un.onInit' in script
        assert f'FileOpen $0 "$INSTDIR\\{exe}" a' in expanded, (
            'the lock probe must target the payload exe')
        assert f"nsExec::Exec 'taskkill /IM {exe} /T /F'" in expanded, (
            'the auto-close must hide its console (nsExec) and take the '
            'whole tree (the agent ssh tunnels die with it)')
        assert '$(TOFU_RUNNING_PROMPT)' in script
    # Bilingual prompt (the installer declares English + SimpChinese).
    assert 'LangString TOFU_RUNNING_PROMPT 1033' in _NSI
    assert 'LangString TOFU_RUNNING_PROMPT 2052' in _NSI
    # ── Inno side (CI): pinned, not defaulted ──
    assert _WORKFLOW.count('CloseApplications=yes') == 2, (
        'both Inno scripts (full + agent) must pin CloseApplications')
    assert _WORKFLOW.count('RestartApplications=no') == 2


# ═══════════════════════════════════════════════════════════════════
#  The 2026-08-04 modernization ratchets (custom nsDialogs wizard)
# ═══════════════════════════════════════════════════════════════════

def test_no_mui_remains():
    """The classic MUI2 wizard is gone — a reintroduction means someone
    reverted the redesign without reading this contract."""
    assert 'MUI2.nsh' not in _NSI
    assert 'MUI_' not in _code(_NSI), (
        'MUI defines/pages found in the custom-UI template — the whole '
        'point of the 2026-08-04 rewrite is that MUI is gone')


def test_solid_lzma_is_the_compressor():
    """Speed contract (measured 2026-08-04): /SOLID lzma takes the agent
    installer 53.2 → 45.2 MB and the full one ~153 → 120 MB, and solid-
    block decompression beats 3316 per-file zlib streams at install time."""
    assert 'SetCompressor /SOLID lzma' in _NSI


def test_dpi_awareness_is_declared():
    """HiDPI contract: blurry 200% text is the other half of "looks 2000s"."""
    assert 'ManifestDPIAware true' in _NSI


def test_bilingual_without_mui():
    """MUI_LANGUAGE is gone; the languages must still be declared so the
    wizard auto-picks the OS language and the NLF buttons localize."""
    assert r'Language files\English.nlf' in _NSI
    assert r'Language files\SimpChinese.nlf' in _NSI
    # Every LangString exists in BOTH languages (1033 en, 2052 zh) — a
    # zh-only or en-only string is how a Chinese user gets raw English.
    import re as _re
    en = set(_re.findall(r'LangString (TOFU_\w+) 1033', _NSI))
    zh = set(_re.findall(r'LangString (TOFU_\w+) 2052', _NSI))
    assert en and en == zh, (
        f'LangString language asymmetry: en-only={en - zh}, zh-only={zh - en}')


def test_silent_mode_still_installs_and_uninstalls():
    """Automation contract: the classic authoring supported /S. Pages are
    skipped when silent, so the bodies must be callable without UI."""
    on_init = _FULL.split('Function .onInit')[1].split('FunctionEnd')[0]
    assert '${If} ${Silent}' in on_init and 'Call DoInstall' in on_init
    un_init = _FULL.split('Function un.onInit')[1].split('FunctionEnd')[0]
    assert '${If} ${Silent}' in un_init
    assert 'Call un.DoUninstall' in un_init


def test_page_flow_order():
    code = _code(_FULL)
    flow = [l for l in code.splitlines()
            if l.startswith(('Page custom', 'UninstPage custom'))]
    assert flow == [
        'Page custom WelcomePageCreate',
        'Page custom DirPageCreate DirPageLeave',
        'Page custom ProgressPageCreate',
        'Page custom FinishPageCreate FinishPageLeave',
        'UninstPage custom un.ConfirmPageCreate',
        'UninstPage custom un.ProgressPageCreate',
        'UninstPage custom un.FinishPageCreate',
    ], f'wizard page flow drifted: {flow}'


def test_no_per_file_log_pane_and_marquee_progress():
    """Speed contract: the classic details list repainted once per file
    (3316 repaints during File /r). It must stay gone — a marquee bar is
    the progress signal now."""
    assert 'ShowInstDetails nevershow' in _NSI
    assert 'PBM_SETMARQUEE' in _code(_NSI)


def test_modern_font_stack_for_labels():
    """The 2000s look was half typography (MS Shell Dlg 8 serif-ish). Our
    labels must use the 2020s stack, chosen per UI language."""
    assert '"Segoe UI"' in _NSI
    assert '"Microsoft YaHei UI"' in _NSI
    assert '${LANG_SIMPCHINESE}' in _NSI, (
        'the font family must switch on the UI language')
