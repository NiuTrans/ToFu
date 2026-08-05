#!/usr/bin/env python3
"""Unit tests for lib.desktop_agent — permission gates, shell-escape branch
selection, XGA coordinate remapping, and missing-dependency guards.

Pure logic only: pyautogui / pyperclip / psutil are NOT required. Importing
lib.desktop_agent._gui must succeed even when they are absent (the guarded
imports set them to None), and the scaling / dispatch / exec logic is tested
without touching a real screen or shell.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_desktop_agent.py -q
"""

import os
import re
import sys
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════
#  1. Coordinate scaling / remapping (XGA)  — the red-capable core
# ══════════════════════════════════════════════════════════

def test_compute_scale_downscales_to_xga():
    from lib.desktop_agent._scaling import compute_scale
    # 1920x1080 must fit within 1024x768: limiting ratio is width 1024/1920.
    scale = compute_scale(1920, 1080)
    assert abs(scale - (1024 / 1920)) < 1e-9


def test_compute_scale_never_upscales_small_screens():
    from lib.desktop_agent._scaling import compute_scale
    # A display already <= XGA is left at native size (no coordinate xlate).
    assert compute_scale(800, 600) == 1.0
    assert compute_scale(1024, 768) == 1.0


def test_compute_scale_degenerate_inputs():
    from lib.desktop_agent._scaling import compute_scale
    assert compute_scale(0, 0) == 1.0
    assert compute_scale(-5, 100) == 1.0


def test_scaled_dimensions_1920_to_xga():
    from lib.desktop_agent._scaling import scaled_dimensions
    w, h, scale = scaled_dimensions(1920, 1080)
    # Width pinned to 1024; height scaled by the same factor.
    assert w == 1024
    assert h == round(1080 * (1024 / 1920))  # 576
    assert h == 576


def test_api_to_real_remaps_model_click_back_to_real_pixels():
    """The headline grounding fix: a model that clicks (512, 288) on the
    1024x576 downscaled image of a 1920x1080 screen must land on the REAL
    pixel (960, 540) — the true screen centre. This is the assertion that
    goes RED if the remap is dropped or inverted."""
    from lib.desktop_agent._scaling import compute_scale, api_to_real
    scale = compute_scale(1920, 1080)          # 1024/1920 ≈ 0.5333
    real_x, real_y = api_to_real(512, 288, scale)
    assert (real_x, real_y) == (960, 540)


def test_real_to_api_is_inverse_of_api_to_real():
    from lib.desktop_agent._scaling import compute_scale, api_to_real, real_to_api
    scale = compute_scale(2560, 1440)
    # Round-trip a real coordinate through both maps; should return close.
    ax, ay = real_to_api(1280, 720, scale)
    rx, ry = api_to_real(ax, ay, scale)
    assert abs(rx - 1280) <= 1 and abs(ry - 720) <= 1


def test_remap_is_identity_when_screen_below_xga():
    from lib.desktop_agent._scaling import compute_scale, api_to_real
    scale = compute_scale(800, 600)  # 1.0 — model sees native image
    assert api_to_real(400, 300, scale) == (400, 300)


def test_api_to_real_guards_zero_scale():
    from lib.desktop_agent._scaling import api_to_real
    assert api_to_real(100, 50, 0) == (100, 50)
    assert api_to_real(100, 50, None) == (100, 50)


def test_target_size_env_override(monkeypatch):
    from lib.desktop_agent import _scaling
    monkeypatch.setenv('TOFU_DESKTOP_TARGET_W', '1280')
    monkeypatch.setenv('TOFU_DESKTOP_TARGET_H', '800')
    assert _scaling.target_size() == (1280, 800)


def test_target_size_bad_env_keeps_default(monkeypatch):
    from lib.desktop_agent import _scaling
    monkeypatch.setenv('TOFU_DESKTOP_TARGET_W', 'not-an-int')
    monkeypatch.delenv('TOFU_DESKTOP_TARGET_H', raising=False)
    assert _scaling.target_size() == (1024, 768)


# ══════════════════════════════════════════════════════════
#  2. dispatch_command permission gates (write / exec / gui)
# ══════════════════════════════════════════════════════════

_NO_PERMS = {'allow_write': False, 'allow_exec': False, 'allow_gui': False}


def test_dispatch_unknown_command():
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_does_not_exist', {}, _NO_PERMS)
    assert 'Unknown command' in res['error']


def test_dispatch_write_gate_blocks_without_allow_write():
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_write_file', {'path': '/tmp/x', 'content': 'y'}, _NO_PERMS)
    assert '--allow-write' in res['error']


def test_dispatch_exec_gate_blocks_without_allow_exec():
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_run_command', {'command': 'ls'}, _NO_PERMS)
    assert '--allow-exec' in res['error']


def test_dispatch_gui_gate_blocks_without_allow_gui():
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_gui_action', {'action': 'click'}, _NO_PERMS)
    assert '--allow-gui' in res['error']


def test_dispatch_gate_lets_permitted_command_through():
    """With allow_exec set, the run gate is passed and the REAL handler runs —
    a trivial echo confirms the dispatcher forwarded to it, not the gate."""
    from lib.desktop_agent._dispatch import dispatch_command
    perms = {'allow_write': False, 'allow_exec': True, 'allow_gui': False}
    res = dispatch_command('desktop_run_command', {'command': 'echo tofu_gate_ok'}, perms)
    assert 'error' not in res or not res['error']
    assert 'tofu_gate_ok' in res.get('stdout', '')


def test_dispatch_screenshot_gate_needs_gui():
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_screenshot', {}, _NO_PERMS)
    assert '--allow-gui' in res['error']


# ══════════════════════════════════════════════════════════
#  3. cmd_run_local — shell-metachar branch selection & safety
# ══════════════════════════════════════════════════════════

def test_shell_meta_regex_detects_pipe_and_redirect():
    from lib.desktop_agent._exec import _SHELL_META_RE
    assert _SHELL_META_RE.search('cat a | grep b')
    assert _SHELL_META_RE.search('echo x > f')
    assert _SHELL_META_RE.search('a && b')


def test_simple_command_uses_argv_not_shell():
    """A plain 'git status' (has a space → meta) vs a single token. A single
    executable token must be split with shlex, NOT routed through the shell."""
    from lib.desktop_agent import _exec
    captured = {}

    class _Res:
        stdout, stderr, returncode = 'ok', '', 0

    def _fake_run(args, **kwargs):
        captured['args'] = args
        captured['shell'] = kwargs.get('shell')
        return _Res()

    with mock.patch.object(_exec.subprocess, 'run', _fake_run):
        _exec.cmd_run_local({'command': 'whoami'})
    assert captured['args'] == ['whoami']
    assert captured['shell'] is False


def test_metachar_command_routed_through_explicit_shell():
    """A piped command must be handed to an explicit ['/bin/sh','-c',cmd]
    argv (never shell=True), so the command is one unambiguous argument."""
    from lib.desktop_agent import _exec
    captured = {}

    class _Res:
        stdout, stderr, returncode = 'ok', '', 0

    def _fake_run(args, **kwargs):
        captured['args'] = args
        captured['shell'] = kwargs.get('shell')
        return _Res()

    with mock.patch.object(_exec.subprocess, 'run', _fake_run):
        _exec.cmd_run_local({'command': 'cat f | wc -l'})
    assert captured['shell'] is False
    # Last element is the full command string, passed to the shell verbatim.
    assert captured['args'][-1] == 'cat f | wc -l'
    assert '-c' in captured['args']


def test_run_local_rejects_empty_command():
    from lib.desktop_agent._exec import cmd_run_local
    assert 'error' in cmd_run_local({'command': '   '})
    assert 'error' in cmd_run_local({'command': ''})


# ══════════════════════════════════════════════════════════
#  4. Missing-dependency guards — handlers degrade, never crash
# ══════════════════════════════════════════════════════════

def test_gui_handlers_return_clear_hint_when_pyautogui_missing():
    from lib.desktop_agent import _gui
    with mock.patch.object(_gui, 'pyautogui', None):
        r1 = _gui.cmd_screenshot_desktop({})
        r2 = _gui.cmd_gui_action({'action': 'click', 'x': 1, 'y': 1})
    for r in (r1, r2):
        assert 'not enabled' in r['error']
        assert 'pyautogui' in r['error']


def test_clipboard_handler_hint_when_pyperclip_missing():
    from lib.desktop_agent import _gui
    with mock.patch.object(_gui, 'pyperclip', None):
        r = _gui.cmd_clipboard({'action': 'read'})
    assert 'pyperclip' in r['error']


def test_system_info_handler_hint_when_psutil_missing():
    from lib.desktop_agent import _gui
    with mock.patch.object(_gui, 'psutil', None):
        r = _gui.cmd_system_info({'type': 'overview'})
    assert 'psutil' in r['error']



# ══════════════════════════════════════════════════════════
#  5. INTEGRATION: screenshot↔click coordinate loop via real dispatch
# ══════════════════════════════════════════════════════════

class _FakePyAutoGUI:
    """Minimal pyautogui stand-in driving the two halves through their OWN
    independent scale derivations: screenshot() returns a real 1920x1080 PIL
    image (so cmd_screenshot_desktop derives scale from image dims), and
    size() reports the same physical screen (so cmd_gui_action derives scale
    from pyautogui.size()). It records the coords click() actually receives."""

    def __init__(self, screen_w=1920, screen_h=1080):
        self._w, self._h = screen_w, screen_h
        self.FAILSAFE = True
        self.PAUSE = 0.0
        self.clicks = []

    def screenshot(self, region=None):
        from PIL import Image
        if region:
            _, _, w, h = region
            return Image.new('RGB', (w, h), (10, 20, 30))
        return Image.new('RGB', (self._w, self._h), (10, 20, 30))

    def size(self):
        return (self._w, self._h)

    def click(self, x=0, y=0, button='left', clicks=1):
        self.clicks.append((x, y, button, clicks))


def test_screenshot_click_coordinate_loop_end_to_end():
    """The end-to-end grounding contract, exercised via dispatch_command (NOT
    by calling _scaling directly): the screenshot half downscales a 1920x1080
    screen and REPORTS a scale; the model picks the centre of that downscaled
    image (512,288); the click half — deriving scale INDEPENDENTLY from
    pyautogui.size() — must land on the true screen centre (960,540). Only a
    test that runs both halves through their own scale derivation can catch a
    divergence between them; the pure _scaling tests share one call and cannot."""
    from lib.desktop_agent import _gui
    from lib.desktop_agent._dispatch import dispatch_command

    fake = _FakePyAutoGUI(1920, 1080)
    perms = {'allow_write': False, 'allow_exec': False, 'allow_gui': True}

    with mock.patch.object(_gui, 'pyautogui', fake):
        # 1) Screenshot half — reports the scale + downscaled dims it produced.
        shot = dispatch_command('desktop_screenshot', {}, perms)
        assert 'error' not in shot, shot
        assert (shot['real_width'], shot['real_height']) == (1920, 1080)
        assert (shot['width'], shot['height']) == (1024, 576)  # XGA-fit
        scale = shot['scale']

        # 2) Model clicks the CENTRE of the image it was shown (1024x576).
        model_x, model_y = shot['width'] // 2, shot['height'] // 2  # (512, 288)
        # It may echo the scale it saw; the click half must still work whether
        # or not it does — here we send the model-space coords with no override
        # so cmd_gui_action re-derives the scale from pyautogui.size().
        act = dispatch_command(
            'desktop_gui_action',
            {'action': 'click', 'x': model_x, 'y': model_y},
            perms,
        )
        assert act.get('success') is True, act

    # 3) The REAL pixel pyautogui was told to click must be the screen centre.
    assert fake.clicks, 'click() was never called'
    real_x, real_y, _, _ = fake.clicks[-1]
    assert (real_x, real_y) == (960, 540), (
        f'click landed at {(real_x, real_y)}, expected screen centre (960, 540) '
        f'— screenshot reported scale={scale}'
    )


# ══════════════════════════════════════════════════════════
#  6. INTEGRATION: run_agent stop_event terminates the poll loop
# ══════════════════════════════════════════════════════════

def test_run_agent_stop_event_terminates_loop():
    """The tray "off" contract: setting stop_event must make run_agent's poll
    loop exit within a bounded number of iterations, not hang. Goes RED if the
    stop_event check is removed or moved after a blocking wait."""
    import threading
    from lib.desktop_agent import _run

    stop_event = threading.Event()
    calls = {'n': 0}

    class _Resp:
        status_code = 200
        def json(self):
            return {'commands': []}

    def _fake_post(url, **kwargs):
        calls['n'] += 1
        # Signal stop after the first successful poll; the loop must then exit
        # at its next top-of-loop check.
        stop_event.set()
        # Safety valve: if the loop ignores stop_event and keeps polling, abort
        # loudly rather than spinning forever.
        if calls['n'] > 50:
            raise AssertionError('run_agent ignored stop_event (>50 polls)')
        return _Resp()

    with mock.patch.object(_run.requests, 'post', _fake_post), \
         mock.patch.object(_run.time, 'sleep', lambda *_a, **_k: None):
        t = threading.Thread(
            target=_run.run_agent,
            args=('http://127.0.0.1:15000', {'allow_write': False,
                                             'allow_exec': False,
                                             'allow_gui': False}),
            kwargs={'poll_interval': 0.001, 'stop_event': stop_event},
            daemon=True,
        )
        t.start()
        t.join(timeout=3.0)

    assert not t.is_alive(), 'run_agent did not terminate after stop_event was set'
    assert calls['n'] >= 1, 'poll loop never ran'
    assert calls['n'] <= 3, f'loop kept polling after stop_event ({calls["n"]} times)'



# ══════════════════════════════════════════════════════════
#  7. Permission policy — deny-by-default + build_permissions
# ══════════════════════════════════════════════════════════

def test_safe_default_is_read_only():
    """Enabling the agent alone must grant NO write/exec/gui tier."""
    from lib.desktop_agent._permissions import SAFE_DEFAULT
    assert SAFE_DEFAULT == {'allow_write': False, 'allow_exec': False,
                            'allow_gui': False, 'allow_egress': False}
    assert not any(SAFE_DEFAULT.values())


def test_safe_default_returns_fresh_mutable_copy():
    from lib.desktop_agent._permissions import safe_default, SAFE_DEFAULT
    a = safe_default()
    a['allow_gui'] = True
    # Mutating the copy must not poison the module constant or a second copy.
    assert SAFE_DEFAULT['allow_gui'] is False
    assert safe_default()['allow_gui'] is False


def test_build_permissions_defaults_deny_all():
    from lib.desktop_agent._permissions import build_permissions
    assert build_permissions() == {'allow_write': False, 'allow_exec': False,
                                   'allow_gui': False, 'allow_egress': False}


def test_build_permissions_individual_tiers():
    from lib.desktop_agent._permissions import build_permissions
    p = build_permissions(allow_write=True, allow_gui=1)  # truthy coerced to bool
    assert p == {'allow_write': True, 'allow_exec': False,
                 'allow_gui': True, 'allow_egress': False}


def test_build_permissions_allow_all_overrides_every_tier():
    from lib.desktop_agent._permissions import build_permissions
    p = build_permissions(allow_all=True)
    assert p == {'allow_write': True, 'allow_exec': True,
                 'allow_gui': True, 'allow_egress': True}


# ══════════════════════════════════════════════════════════
#  8. desktop_system_info type=kill gate (adjacent security bug)
# ══════════════════════════════════════════════════════════

def test_system_info_read_only_types_need_no_permission():
    """overview / processes are read-only — allowed under the deny-all default.
    (psutil is absent here, so they reach the handler's dep-guard, proving the
    dispatcher did NOT block them.)"""
    from lib.desktop_agent._dispatch import dispatch_command
    for t in ('overview', 'processes'):
        res = dispatch_command('desktop_system_info', {'type': t}, dict(_NO_PERMS))
        # NOT a permission rejection — it fell through to the handler.
        assert 'requires --allow-exec' not in res.get('error', '')


def test_system_info_kill_is_gated_behind_allow_exec():
    """type=kill terminates a process — it MUST be blocked without allow_exec.
    Red-verified: dropping the kill gate in dispatch_command lets this through."""
    from lib.desktop_agent._dispatch import dispatch_command
    res = dispatch_command('desktop_system_info',
                           {'type': 'kill', 'pid': 4242}, dict(_NO_PERMS))
    assert 'requires --allow-exec' in res['error']


def test_system_info_kill_allowed_with_allow_exec():
    """With allow_exec the kill gate passes — it then reaches the handler
    (which dep-errors on missing psutil, proving the gate let it by)."""
    from lib.desktop_agent import _gui
    from lib.desktop_agent._dispatch import dispatch_command
    perms = {'allow_write': False, 'allow_exec': True, 'allow_gui': False}
    with mock.patch.object(_gui, 'psutil', None):
        res = dispatch_command('desktop_system_info',
                               {'type': 'kill', 'pid': 4242}, perms)
    assert 'requires --allow-exec' not in res.get('error', '')
    assert 'psutil' in res['error']  # reached the handler's dep guard


# ══════════════════════════════════════════════════════════
#  9. Live permission mutation — tray toggle takes effect w/o restart
# ══════════════════════════════════════════════════════════

def test_live_perm_dict_mutation_is_picked_up_by_dispatch():
    """The tray mutates ONE shared perms dict in place; run_agent passes that
    same dict to dispatch_command every poll. Flipping a tier on the live dict
    must change the very next dispatch decision — no agent restart needed."""
    from lib.desktop_agent._dispatch import dispatch_command
    from lib.desktop_agent._permissions import safe_default

    perms = safe_default()  # deny-all, the shared object
    # Before: exec denied.
    blocked = dispatch_command('desktop_run_command', {'command': 'echo hi'}, perms)
    assert '--allow-exec' in blocked['error']

    # Tray ticks "Allow run commands" — mutate the SAME dict in place.
    perms['allow_exec'] = True
    allowed = dispatch_command('desktop_run_command', {'command': 'echo live_ok'}, perms)
    assert 'live_ok' in allowed.get('stdout', '')


# ══════════════════════════════════════════════════════════
#  10. Dependency-list drift guard — the root cause of the ORIGINAL bug
# ══════════════════════════════════════════════════════════
#
# The head-line "users can't use it at all" bug was a runtime dep present in
# ZERO of the packaging lists. Those deps are now maintained in THREE
# hand-edited parallel places with nothing binding them:
#   (a) desktop/requirements-desktop.txt   — pip install for source/manual
#   (b) tofu.spec hidden_imports           — what PyInstaller freezes into exe
#   (c) build-desktop.yml `pip install …`  — what CI installs before building
#       (there are MULTIPLE such lines — one per platform job; ALL must have it)
# If any single list silently drops one dep, the frozen build ships broken
# again with no other test to catch it. This guard parses the REAL files (no
# hardcoded copy of the dep list) and asserts each runtime dep is in ALL of
# them. It goes RED naming the offending list if one drifts.

# The runtime deps the desktop-control agent imports (lib/desktop_agent/_gui).
# NOT the packaging-only tools (pyinstaller/pystray) — those aren't imported by
# the agent at runtime, so they are out of scope for this guard.
_DESKTOP_RUNTIME_DEPS = ('pyautogui', 'pyperclip', 'psutil')


def _read(rel):
    with open(os.path.join(_REPO_ROOT, rel), encoding='utf-8') as f:
        return f.read()


def _requirements_pkgs(text):
    """Package names (lowercased) declared in a requirements .txt (strip the
    version spec + comments/blanks)."""
    import re
    pkgs = set()
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        name = re.split(r'[<>=!~ \[]', line, 1)[0].strip().lower()
        if name:
            pkgs.add(name)
    return pkgs


def _spec_hidden_imports(text):
    """Module names appended to hidden_imports in tofu.spec — parsed from the
    real source via AST so a moved/renamed literal can't fool a substring check.
    Collects every string literal in any `hidden_imports += [...]` / `+= [..]`
    or assignment whose target is `hidden_imports`."""
    import ast
    tree = ast.parse(text)
    names = set()

    def _collect_list(node):
        if isinstance(node, ast.List):
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    names.add(elt.value.lower())

    for node in ast.walk(tree):
        # hidden_imports += [ ... ]
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == 'hidden_imports':
            _collect_list(node.value)
        # hidden_imports = [ ... ]
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == 'hidden_imports':
                    _collect_list(node.value)
    return names


_STEP_HEADER_RE = re.compile(r'^      - name: ', re.M)


def _ci_install_step_pip_pkgs(text):
    """One package-token set per 'Install dependencies' step: the union of
    every `pip install` line in that step (excluding `-r requirements.txt`).

    Semantics correction (2026-08-01, pt_0720694046c042e6): the guard used to
    assert EVERY pip install LINE carried the runtime deps. That assumed all
    install lines are the same KIND — broken by the vendor-wheel line
    (`pip install --no-deps vendor/tofu_search-*.whl`), which is deliberately
    dep-less: its deps come from the requirements.txt solve right after, and
    giving pip deps there would let it pull versions from the index AHEAD of
    the frozen solve. What a platform leg actually needs is: after ALL of the
    step's install lines, the env contains the runtime deps. Aggregate per
    step, never per line.
    """
    heads = [m.start() for m in _STEP_HEADER_RE.finditer(text)]
    steps = []
    for i, start in enumerate(heads):
        header_end = text.index('\n', start) + 1
        end = heads[i + 1] if i + 1 < len(heads) else len(text)
        if 'Install dependencies' not in text[start:header_end]:
            continue
        pkgs = set()
        for raw in text[header_end:end].splitlines():
            s = raw.strip()
            if s.startswith('pip install') and '-r ' not in s:
                pkgs.update(w.lower() for w in s.split())
        steps.append(pkgs)
    return steps


def test_desktop_runtime_deps_present_in_requirements_file():
    pkgs = _requirements_pkgs(_read('desktop/requirements-desktop.txt'))
    missing = [d for d in _DESKTOP_RUNTIME_DEPS if d not in pkgs]
    assert not missing, (
        f'desktop/requirements-desktop.txt is missing runtime dep(s): {missing}. '
        f'The frozen/manual install would ship without them — the original bug.'
    )


def test_desktop_runtime_deps_present_in_pyinstaller_spec():
    hidden = _spec_hidden_imports(_read('tofu.spec'))
    missing = [d for d in _DESKTOP_RUNTIME_DEPS if d not in hidden]
    assert not missing, (
        f"tofu.spec hidden_imports is missing runtime dep(s): {missing}. "
        f"PyInstaller would not freeze them into the exe."
    )


def test_desktop_runtime_deps_present_in_every_ci_pip_install():
    """Every CI install STEP's aggregated packages cover the runtime deps.

    (Name kept as the historical handle; the unit of assertion is the STEP,
    not the line — see _ci_install_step_pip_pkgs for why.)
    """
    text = _read('.github/workflows/build-desktop.yml')
    steps = _ci_install_step_pip_pkgs(text)
    # Sanity: the workflow really has install steps (one per platform job).
    # If this drops the parse broke — fail loudly, never vacuously green.
    assert len(steps) == 3, (
        f'expected exactly 3 "Install dependencies" steps in build-desktop.yml '
        f'(one per platform job), found {len(steps)}: {steps}'
    )
    for i, pkgs in enumerate(steps):
        missing = [d for d in _DESKTOP_RUNTIME_DEPS if d not in pkgs]
        assert not missing, (
            f'build-desktop.yml install step #{i + 1} is missing runtime '
            f'dep(s): {missing}. That platform build would ship broken.\n'
            f'  aggregated packages: {sorted(pkgs)}'
        )


_SYNTH_STEP_DEPS = (
    '      - name: Install dependencies\n'
    '        run: |\n'
    '          if ls vendor/tofu_search-*.whl >/dev/null 2>&1; then\n'
    '            pip install --no-deps vendor/tofu_search-*.whl\n'
    '          fi\n'
    '          pip install -r requirements.txt\n'
    '          pip install pyinstaller pystray pyautogui pyperclip psutil\n'
    '\n'
    '      - name: Generate icons\n'
    '        run: python scripts/gen_desktop_icons.py\n'
)


def test_step_aggregate_tolerates_a_dep_less_pip_line():
    """The semantics correction, pinned against regression: the vendor-wheel
    `--no-deps` line in a step whose OTHER line carries the deps must NOT
    trip the guard. (This is exactly the workflow shape that went red.)
    """
    steps = _ci_install_step_pip_pkgs(_SYNTH_STEP_DEPS)
    assert len(steps) == 1
    missing = [d for d in _DESKTOP_RUNTIME_DEPS if d not in steps[0]]
    assert not missing, (
        f'the aggregate must see the deps through the dep-less line: {missing}')


def test_step_aggregate_catches_a_leg_that_lost_its_deps():
    """Complement: a leg whose explicit deps line vanished must still go
    red — the vendor line alone can never satisfy the runtime deps."""
    text = _SYNTH_STEP_DEPS.replace(
        '          pip install pyinstaller pystray pyautogui pyperclip psutil\n',
        '')
    steps = _ci_install_step_pip_pkgs(text)
    assert len(steps) == 1
    missing = [d for d in _DESKTOP_RUNTIME_DEPS if d not in steps[0]]
    assert missing == list(_DESKTOP_RUNTIME_DEPS), (
        f'a leg with only the vendor line must be flagged: missing={missing}')


def test_all_three_dep_lists_agree_on_runtime_deps():
    """Single assertion over all three sources — the drift guard proper. If any
    ONE list drops a dep, this names which source and which dep.

    The CI side contributes one entry per install STEP (aggregated — see
    _ci_install_step_pip_pkgs); a single dep-less line inside a step is fine
    as long as the step as a whole installs the deps.
    """
    sources = {
        'desktop/requirements-desktop.txt': _requirements_pkgs(
            _read('desktop/requirements-desktop.txt')),
        'tofu.spec:hidden_imports': _spec_hidden_imports(_read('tofu.spec')),
    }
    ci_text = _read('.github/workflows/build-desktop.yml')
    for i, pkgs in enumerate(_ci_install_step_pip_pkgs(ci_text)):
        sources[f'build-desktop.yml:step#{i + 1}'] = pkgs

    drift = {}
    for src, pkgs in sources.items():
        gone = [d for d in _DESKTOP_RUNTIME_DEPS if d not in pkgs]
        if gone:
            drift[src] = gone
    assert not drift, f'dependency-list drift detected: {drift}'
