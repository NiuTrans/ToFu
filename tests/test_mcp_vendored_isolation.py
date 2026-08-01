"""tests/test_mcp_vendored_isolation.py — vendored servers must launch ISOLATED.

WHY THIS GUARD EXISTS (measured 2026-07-31, epic pt_9345a80f417d43ca)
---------------------------------------------------------------------
hope-mcp / llm-mcp / xuecheng-mcp were pip-installed into Tofu's OWN
interpreter (``_install.py`` used ``sys.executable -m pip install``). That made
the server's ``mcp`` and the Tofu client's ``mcp`` ONE package in ONE
resolution: any server needing mcp 2.x would upgrade the SDK out from under
the v1 client (AttributeError at import, the overleaf failure class), and the
client migrating to v2 would break every v1-API server sharing the env. A
global ``mcp<2`` pin was the symptom-management for that coupling.

The fix launches every vendored server via
``uv run --no-project --with-editable <resolved source>`` so each server gets
its OWN environment and its OWN ``mcp``. Measured properties of this shape:

  * isolation: the server's dependency tree never touches Tofu's interpreter;
  * freshness: ``--with-editable`` links the source tree, so edits are live on
    the next connect. ``uvx --from <dir>`` was measured to serve a STALE
    cached build even with ``--refresh`` / ``--reinstall`` (a file created in
    the source was absent from the installed package) — that failure class is
    exactly why editable was chosen over uvx;
  * reproducibility: ``UV_EXCLUDE_NEWER`` (injected by
    ``_ensure_writable_caches``) still bounds the resolution.

The pip-into-shared-interpreter machinery is DELETED, not deprecated — a
dormant path back into the shared env is how the coupling would silently
return.
"""

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lib.mcp.client as mc  # noqa: E402
from lib.mcp.vendored import VENDORED_LAUNCHERS  # noqa: E402

pytestmark = pytest.mark.unit

_INTERNAL = ('hope-mcp', 'llm-mcp', 'xuecheng-mcp')


def test_vendored_registry_covers_all_three_internal_servers():
    """All three internal servers are registered — xuecheng-mcp was MISSING.

    The old registry only listed hope-mcp and llm-mcp; xuecheng-mcp reached
    PATH only via install.sh's pip step, so the auto-install machinery could
    not see it. Isolation that skips one server is not isolation.
    """
    for name in _INTERNAL:
        assert name in VENDORED_LAUNCHERS, (
            f'{name} is not in VENDORED_LAUNCHERS — it would keep launching '
            f'from the shared interpreter (or not at all on a fresh deploy)'
        )
        sources = VENDORED_LAUNCHERS[name]['sources']
        assert any(s.startswith('tools/') for s in sources), (
            f'{name} has no tools/ snapshot source — fresh checkouts would '
            f'have nothing to launch'
        )


def test_vendored_launch_argv_is_uv_run_editable():
    """The launch shape is uv run --with-editable against a REAL source dir.

    This is the single mechanism that decouples the server's mcp from Tofu's.
    If it returns None, or drops --with-editable, or points at a directory
    without a pyproject.toml, the isolation either does not happen or serves
    stale code.
    """
    from lib.mcp.client import vendored_launch_argv

    for name in _INTERNAL:
        argv = vendored_launch_argv(name)
        if argv is None:
            pytest.skip(f'no source for {name} on this machine '
                        f'(sibling/vendor/tools all absent)')
        assert argv[0] == 'uv', f'{name}: launcher must be uv, got {argv[0]!r}'
        assert argv[1:3] == ['run', '--no-project'], (
            f'{name}: must run OUTSIDE the chatui project env, got {argv[1:3]}'
        )
        assert '--with-editable' in argv, (
            f'{name}: --with-editable missing — uvx-style cached builds were '
            f'measured to serve STALE code (a file created in the source was '
            f'absent from the installed package even with --refresh)'
        )
        src = argv[argv.index('--with-editable') + 1]
        assert argv[-1] == name, (
            f'{name}: argv must end with the console script name, got {argv[-1]!r}'
        )
        assert os.path.isfile(os.path.join(src, 'pyproject.toml')), (
            f'{name}: resolved source {src} has no pyproject.toml'
        )

    # Non-vendored commands must NOT be translated.
    assert vendored_launch_argv('definitely-not-a-server') is None
    # Commands with a path separator are taken as-is, never translated.
    assert vendored_launch_argv('/usr/bin/hope-mcp') is None


def test_bridge_translates_vendored_command_inside_owner():
    """The translation must happen in _server_owner, before PATH resolution.

    A helper that exists but is never called leaves the production coupling
    fully intact while every unit test above stays green — the
    "tested-but-not-wired" failure class. Assert on the OWNER's source: it
    must call vendored_launch_argv, and it must NOT reference the deleted
    pip auto-install path.
    """
    from lib.mcp.client._bridge import MCPBridge

    src = inspect.getsource(MCPBridge._server_owner)
    assert 'vendored_launch_argv' in src, (
        '_server_owner never calls vendored_launch_argv — vendored servers '
        'still resolve via the shared interpreter'
    )
    assert '_try_autoinstall_launcher' not in src, (
        '_server_owner still references the pip auto-install fallback — the '
        'coupling path is still reachable'
    )


def test_pip_into_shared_interpreter_machinery_is_deleted():
    """pip-install-into-sys.executable must be GONE, not dormant.

    Leaving the machinery around means some future path (or a well-meaning
    revert) re-couples a server's SDK to Tofu's. The only pip references that
    may survive in _install.py are inert hint text.
    """
    import lib.mcp.client._install as inst

    assert not hasattr(mc, '_try_autoinstall_launcher'), (
        '_try_autoinstall_launcher is still exported on the facade'
    )
    assert not hasattr(inst, '_run_pip_install'), (
        '_run_pip_install still exists in _install.py'
    )
    src = inspect.getsource(inst)
    assert "'-m', 'pip', 'install'" not in src, (
        "_install.py still shells out to `python -m pip install` into the "
        "shared interpreter"
    )


def test_prewarm_uses_uv_warm_not_pip():
    """prewarm_vendored_launcher must warm the uv env, not pip-install.

    The install route's zero-touch flow (start_install_job -> ready) survives
    the migration only if prewarm actually resolves the isolated env. A pip
    call here would re-couple; a no-op here would push the cold resolve into
    the first connect's readiness budget.
    """
    from lib.mcp.client import prewarm_vendored_launcher

    src = inspect.getsource(prewarm_vendored_launcher)
    assert 'vendored_launch_argv' in src, (
        'prewarm does not resolve the isolated launch argv — it cannot be '
        'warming the uv env'
    )
    assert 'subprocess.run' in src, (
        'prewarm never runs the warm subprocess — the cold resolve would land '
        'inside the first connect readiness budget instead'
    )
    assert '_try_autoinstall_launcher' not in src, (
        'prewarm still calls the pip auto-install path'
    )
    assert "'-m', 'pip'" not in src and '-m pip install' not in src, (
        'prewarm still shells out to pip — docstring prose mentioning pip is '
        'fine, the ACTION is what must be gone'
    )


def test_install_script_warms_instead_of_pip_installing():
    """install.sh must not pip-install bundled servers into the conda env.

    The bundled-MCP step was a SECOND coupling path (deploy-time): it
    pip-installed hope/xuecheng/llm into Tofu's interpreter, so even a deploy
    that never used auto-install was coupled from minute zero.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, 'install.sh'), encoding='utf-8') as f:
        text = f.read()
    assert '_safe_pip_install --upgrade "${_BUNDLED_MCPS' not in text, (
        'install.sh still pip-installs the bundled MCP servers into the '
        'shared conda env'
    )
    assert '--with-editable' in text, (
        'install.sh does not warm the isolated env for bundled servers — '
        'fresh deploys would cold-resolve on first connect'
    )


def test_tools_pyproject_comments_do_not_claim_shared_install():
    """The vendored servers' pyproject comments must not teach the coupling.

    tools/hope-mcp/pyproject.toml justifies its mcp pin with "this package is
    pip-installed into TOFU'S OWN interpreter" — FALSE after this migration.
    A comment describing a deleted mechanism instructs the next agent to
    preserve a problem that no longer exists.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ('hope-mcp', 'llm-mcp', 'github-batch-mcp'):
        pj = os.path.join(repo, 'tools', name, 'pyproject.toml')
        if not os.path.isfile(pj):
            continue
        with open(pj, encoding='utf-8') as f:
            text = f.read().lower()
        assert "tofu's own interpreter" not in text and \
               'tofu’s own interpreter' not in text, (
            f'tools/{name}/pyproject.toml still claims it is pip-installed '
            f'into the shared interpreter — update the justification comment'
        )
