"""tests/test_gate.py — the trading_enabled flag must actually gate the feature.

Regression cover for a defect where the flag was declared but never READ:
``web.register()`` returned its blueprints, ``start_workers()`` started its
threads, and nothing anywhere consulted ``lib.TRADING_ENABLED``. Switching the
Settings toggle off therefore changed nothing — the REST surface kept serving
real holdings and the intel crawler kept spending LLM budget unattended.

The cost half is the point of the worker tests: those loops call
``smart_chat_batch`` / ``smart_chat`` on daemon threads with nobody watching.

Run:  pytest tests/test_gate.py -v
"""

from __future__ import annotations

import pytest

pytest.importorskip("lib", reason="Tofu host (core) not installed")


@pytest.fixture
def flag():
    """Set/restore lib.TRADING_ENABLED around each test."""
    import lib
    saved = getattr(lib, "TRADING_ENABLED", False)

    def _set(value):
        lib.TRADING_ENABLED = value

    yield _set
    lib.TRADING_ENABLED = saved


# ── The flag is read LIVE, not snapshotted ────────────────────────────

def test_gate_follows_the_live_flag(flag):
    """A toggle must take effect without a restart — so no caching."""
    from tofu_trading.gate import trading_enabled
    flag(True)
    assert trading_enabled() is True
    flag(False)
    assert trading_enabled() is False
    flag(True)
    assert trading_enabled() is True


def test_gate_fails_closed_when_flag_absent(monkeypatch):
    """A host that never ran the flag registry must read as disabled.

    Failing closed matters here: failing OPEN would let a host with a broken
    flag registry spend LLM budget the operator never opted into.
    """
    import lib
    from tofu_trading.gate import trading_enabled
    monkeypatch.delattr(lib, "TRADING_ENABLED", raising=False)
    assert trading_enabled() is False


# ── Background workers: the cost-safety half ──────────────────────────

def test_worker_blocks_while_disabled_then_resumes(flag):
    """wait_until_enabled must park the worker OFF and release it ON.

    Parking (rather than returning/exiting) is what makes re-enabling work
    without a restart: the threads are created once at boot and never respawn.
    """
    from tofu_trading.gate import wait_until_enabled
    flag(False)
    slept = []

    def fake_sleep(interval):
        slept.append(interval)
        if len(slept) == 3:
            flag(True)          # operator flips it back on mid-wait

    wait_until_enabled(fake_sleep, 5)
    assert slept == [5, 5, 5], "must keep waiting while off, and stop once on"


def test_worker_does_not_block_when_enabled(flag):
    """The happy path must not cost a sleep interval per pass."""
    from tofu_trading.gate import wait_until_enabled
    flag(True)
    calls = []
    wait_until_enabled(lambda i: calls.append(i), 5)
    assert calls == []


def test_both_spending_loops_consult_the_gate():
    """Neither LLM-spending loop may tick without checking the flag.

    Asserted on the AST of the worker factory (a ratchet, not a behaviour
    test): actually running either loop would fan out to the network. The
    anchor is the function body, so it survives refactors that move lines.
    """
    import ast
    from pathlib import Path

    handlers = Path(__file__).parents[1] / 'tofu_trading' / 'web' / 'handlers'
    for filename, factory_name in (
        ('trading_intel.py', 'start_intel_worker'),
        ('trading_autopilot.py', 'start_autopilot_worker'),
    ):
        module = ast.parse((handlers / filename).read_text(encoding='utf-8'))
        factory = next(
            node for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == factory_name
        )
        tree = ast.Module(body=factory.body, type_ignores=[])
        called = {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "wait_until_enabled" in called, (
            f"{factory_name} can spend LLM budget without consulting the "
            f"trading_enabled flag"
        )


# ── Request surface ───────────────────────────────────────────────────

def test_every_api_blueprint_carries_the_gate(flag):
    """Each v1 blueprint must refuse a REAL request while the feature is off.

    Drives an actual request rather than inspecting ``bp.deferred_functions``:
    that attribute is already non-empty from route registration, so asserting
    on it passed even with the guard removed (verified by neutering it). The
    guard is attached in register() rather than per-handler so a newly added
    route inherits it by default.

    The app is built from whichever framework the blueprints were created with
    — under the host's Flask->Quart shim that is Quart, in a bare test session
    it is Flask — because ``lib.api_response.jsonify`` is bound to the same one
    and mixing the two raises "Working outside of application context".
    """
    import sys

    import tofu_trading.web as web

    bps = web.register()
    api_bps = [b for b in bps if b.name.startswith("api_v1_trading")]
    assert api_bps, "no trading API blueprints found"

    framework = sys.modules[type(api_bps[0]).__module__.split(".")[0]]
    App = getattr(framework, "Flask", None) or framework.Quart
    if "PROVIDE_AUTOMATIC_OPTIONS" not in App.default_config:
        # Flask 3.1+ dropped this default but add_url_rule still reads it;
        # server.py injects it the same way before instantiating the app.
        App.default_config = {**App.default_config,
                              "PROVIDE_AUTOMATIC_OPTIONS": True}
    app = App(__name__)
    for bp in api_bps:
        app.register_blueprint(bp)

    flag(False)
    resp = app.test_client().get("/api/v1/trading/holdings")
    status = getattr(resp, "status_code", None)
    if status is None:                      # Quart returns a coroutine
        import asyncio
        status = asyncio.get_event_loop().run_until_complete(resp).status_code
    assert status == 404, (
        "trading API served a request while the feature was switched off"
    )

    # ON: the guard must step aside (checked on the hook itself — running the
    # handler here would hit the DB, which this unit test has no business doing).
    flag(True)
    assert web._reject_when_disabled() is None


def test_page_routes_are_gated(flag):
    """/trading.html and its assets 404 while the feature is off."""
    import tofu_trading.web as web
    flag(False)
    with pytest.raises(Exception) as exc:   # werkzeug NotFound
        web.trading_page()
    assert "404" in str(exc.value) or "Not Found" in str(exc.value)


def test_flag_declares_no_restart_needed():
    """The toggle is hot now; claiming otherwise misinforms the user.

    It was declared needs_restart=True while nothing read the flag at all —
    the UI promised a restart would apply a change that never happened.
    """
    import tofu_trading.flags as flags
    captured = {}
    flags.register(lambda **kw: captured.update(kw))
    assert captured["needs_restart"] is False
