"""tests/test_connect_line_contract.py — the web↔app connect-line contract.

WHY THIS EXISTS
---------------
The remote setup flow is a closed loop across two codebases that never import
each other:

    static/js/local-control.js::_lcConnectLine   (renders the line)
              ↓  user copies one string, pastes it into the tray dialog
    lib/desktop_agent/config.py::parse_connect_line   (consumes the line)

Nothing in either language forces them to agree. Change the separator on the
web side, or tighten the split on the app side, and the *only* symptom is a
user who pastes a valid line and is told it is malformed — with both unit
suites still green, because each half is individually correct.

So this suite deliberately does NOT test the parser against hand-written
strings. It runs the REAL JavaScript formatter under node, feeds its ACTUAL
output to the REAL Python parser, and asserts the values survive. A test that
checked the parser in isolation would keep passing while the two halves
diverge — that is the failure mode being guarded.

(charter: "禁止在测试 harness 里手抄生产判据" — no format literal is written
down here; the string under test is produced by shipped code at run time, and
the JS function is located by SYMBOL so a module split re-points it.)
"""
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _find_defining_file(symbol: str) -> Path:
    """Locate the ONE JS file defining ``function <symbol>(``.

    Symbol-anchored, not path-anchored: a legitimate module split re-points
    this instead of producing an unreadable false red. Three distinguishable
    outcomes, each naming who broke.
    """
    needle = f"function {symbol}("
    hits = [p for p in sorted(JS_DIR.rglob("*.js"))
            # Dotfiles: the bundler's atomic-rename temp files
            # (`.bundle-<hash>.<rand>.js`) are half-written copies —
            # scanning one mid-build is a false red (2026-08-04).
            if not p.name.startswith((".", "bundle-", "feature-", "i18n-"))
            and needle in p.read_text(encoding="utf-8")]
    if not hits:
        raise AssertionError(
            f"IMPLEMENTATION GONE: nothing defines `{needle}`. If the remote "
            f"connect flow was removed, this contract guards nothing — decide "
            f"whether the flow should still exist before repointing.")
    if len(hits) > 1:
        raise AssertionError(
            f"SINGLE SOURCE COPIED: `{needle}` defined in "
            f"{[str(p.relative_to(ROOT)) for p in hits]}; the line format must "
            f"have exactly one producer.")
    return hits[0]


def _slice_fn(symbol: str, override_src: str = "") -> str:
    """Brace-match the named JS function out of its defining file."""
    src = override_src or _find_defining_file(symbol).read_text(encoding="utf-8")
    start = src.index(f"function {symbol}(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"could not brace-match {symbol}")


def _web_emits(server_url: str, token: str, neuter=None) -> str:
    """Run the REAL shipped formatter under node and return what it produced."""
    src = _find_defining_file("_lcConnectLine").read_text(encoding="utf-8")
    if neuter:
        neutered = neuter(src)
        assert neutered != src, "NEUTER substitution did not apply to the JS"
        src = neutered
    fn = _slice_fn("_lcConnectLine", override_src=src)
    script = (fn + "\nconsole.log(_lcConnectLine("
              + repr(server_url).replace("'", '"') + ", "
              + repr(token).replace("'", '"') + "));")
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return proc.stdout.rstrip("\n")


def _parse(line: str):
    from lib.desktop_agent.config import parse_connect_line
    return parse_connect_line(line)


# ── Scan-surface report ────────────────────────────────────────────────

def test_scan_surface_report(capsys):
    """Show which producer/consumer are actually under test before asserting."""
    js = _find_defining_file("_lcConnectLine").relative_to(ROOT)
    from lib.desktop_agent import config as cfg
    with capsys.disabled():
        print("\n[contract] producer:", js, "::_lcConnectLine")
        print("[contract] consumer:",
              Path(cfg.__file__).relative_to(ROOT), "::parse_connect_line")
        print("[contract] sample  :", repr(_web_emits("https://x.test", "tok_1")))
    assert hasattr(cfg, "parse_connect_line")


# ── The contract: what the web emits, the app accepts ──────────────────

def test_what_the_web_emits_is_what_the_app_accepts():
    """The end-to-end loop, driving both real implementations."""
    line = _web_emits("https://tofu.example.com", "tok_ABC123")
    url, secret = _parse(line)
    assert url == "https://tofu.example.com"
    assert secret == "tok_ABC123"


def test_a_trailing_slash_on_the_server_url_survives_the_round_trip():
    """`request.host_url` ends in '/', so this is the COMMON shape, not an edge."""
    line = _web_emits("https://tofu.example.com/", "tok_XYZ")
    url, secret = _parse(line)
    assert url == "https://tofu.example.com"
    assert secret == "tok_XYZ"


def test_a_path_prefixed_server_survives():
    """Reverse-proxied deployments serve Tofu under a sub-path."""
    line = _web_emits("https://corp.example.com/tofu/", "tok_P")
    url, secret = _parse(line)
    assert url == "https://corp.example.com/tofu"
    assert secret == "tok_P"


def test_clipboard_reflow_does_not_break_the_paste():
    """A copied line may pick up wrapping or extra spaces in transit.

    Asserts the RESULT (the user's paste still works) rather than pinning a
    separator, so the web side may re-space the line without breaking the app.
    """
    line = _web_emits("https://tofu.example.com", "tok_R")
    for mangled in (f"  {line}  ", line.replace("  ", " "),
                    line.replace("  ", "\t"), line.replace("  ", "\n")):
        url, secret = _parse(mangled)
        assert url == "https://tofu.example.com" and secret == "tok_R"


# ── Refusals must be actionable, and must never echo the secret ────────

@pytest.mark.parametrize("bad, why", [
    ("tok_ONLY", "a bare token has no address to poll"),
    ("https://tofu.example.com", "an address with no token cannot authenticate"),
    ("", "empty paste"),
    ("ftp://tofu.example.com tok_X", "non-http scheme"),
])
def test_incomplete_pastes_are_refused_with_a_reason(bad, why):
    with pytest.raises(ValueError) as ei:
        _parse(bad)
    assert str(ei.value).strip(), f"refusal for {why} must carry a message"


def test_a_refusal_never_echoes_the_secret():
    """Dialog text can be screenshotted or pasted into a bug report."""
    with pytest.raises(ValueError) as ei:
        _parse("ftp://tofu.example.com tok_SUPERSECRET")
    assert "tok_SUPERSECRET" not in str(ei.value)


# ── Default stays local: the tray case must be untouched ───────────────

def test_no_attachment_means_the_local_server(tmp_path, monkeypatch):
    """An unconfigured packaged app must behave exactly as it did before."""
    monkeypatch.setenv("TOFU_DESKTOP_CONFIG", str(tmp_path / "agent.json"))
    from lib.desktop_agent.config import remote_server
    assert remote_server() == ("", "")


def test_the_attachment_survives_a_restart(tmp_path, monkeypatch):
    """Persisted next to agent_id / share_roots, which already outlive restarts."""
    monkeypatch.setenv("TOFU_DESKTOP_CONFIG", str(tmp_path / "agent.json"))
    from lib.desktop_agent.config import (remote_server, save_remote_server)
    line = _web_emits("https://tofu.example.com/", "tok_PERSIST")
    save_remote_server(*_parse(line))
    assert remote_server() == ("https://tofu.example.com", "tok_PERSIST")


def test_clearing_the_attachment_returns_to_local(tmp_path, monkeypatch):
    """Complement: without this, 'go back to local' would be unreachable."""
    monkeypatch.setenv("TOFU_DESKTOP_CONFIG", str(tmp_path / "agent.json"))
    from lib.desktop_agent.config import (remote_server, save_remote_server)
    save_remote_server("https://tofu.example.com", "tok_1")
    assert remote_server()[0]
    save_remote_server("", "")
    assert remote_server() == ("", "")


def test_saving_an_attachment_preserves_the_agent_identity(tmp_path, monkeypatch):
    """agent_id keys the server-side registry — losing it orphans the device."""
    monkeypatch.setenv("TOFU_DESKTOP_CONFIG", str(tmp_path / "agent.json"))
    from lib.desktop_agent.config import (load_config, save_config,
                                          save_remote_server)
    save_config({"agent_id": "abc123", "share_roots": [{"name": "p",
                                                        "path": "/tmp/p"}]})
    save_remote_server("https://tofu.example.com", "tok_1")
    cfg = load_config()
    assert cfg["agent_id"] == "abc123"
    assert cfg["share_roots"] == [{"name": "p", "path": "/tmp/p"}]


# ── The destination must exist in the tray ─────────────────────────────
# The whole reason this contract exists is that the web UI tells the user to
# paste a line into the desktop app. Before this landed, the app had NO field
# to paste into — a well-worded instruction pointing at a UI that did not
# exist. These assert the destination is reachable, by AST (a mention in a
# comment or docstring must not satisfy them).

def _launcher_ast():
    import ast
    return ast.parse((ROOT / "desktop" / "launcher.py").read_text(encoding="utf-8"))


def _launcher_funcs() -> set:
    import ast
    tree = _launcher_ast()
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_the_tray_has_somewhere_to_paste_the_line():
    """A dialog function must really be defined, not merely referenced."""
    assert "_prompt_connect_line" in _launcher_funcs(), (
        "the tray has no connect dialog — the web instruction would point at "
        "a UI that does not exist")
    assert "on_connect_remote" in _launcher_funcs(), (
        "no tray handler wires the dialog to the menu")


def test_the_connect_entry_is_actually_in_the_menu():
    """Defining the handler is not enough; it must be clickable."""
    src = (ROOT / "desktop" / "launcher.py").read_text(encoding="utf-8")
    menu_start = src.index("menu = pystray.Menu(")
    menu_src = src[menu_start:src.index("icon = pystray.Icon(", menu_start)]
    assert "on_connect_remote" in menu_src, (
        "the connect handler exists but no MenuItem invokes it — the user "
        "cannot reach the dialog")


def test_the_tray_reports_which_server_it_is_attached_to():
    """Silence after pasting left the user unable to tell it worked."""
    src = (ROOT / "desktop" / "launcher.py").read_text(encoding="utf-8")
    menu_start = src.index("menu = pystray.Menu(")
    menu_src = src[menu_start:src.index("icon = pystray.Icon(", menu_start)]
    assert "_attached_url" in menu_src, (
        "the tray menu does not surface the attached server")


def test_the_launcher_delegates_parsing_rather_than_reimplementing_it():
    """Two parsers would drift; the format has exactly one owner.

    AST-asserted (an import node), because a substring check is satisfied by
    a comment that merely names the function. The dialog moved to
    desktop/connect_ui.py (shared with the agent-only build — one
    authoring, never two): the parser import must live THERE, and the
    launcher must reach the dialog through delegation, not re-grow a copy.
    """
    import ast

    def _imports(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    out.add(f"{node.module}.{a.name}")
        return out

    cui = _imports(ROOT / "desktop" / "connect_ui.py")
    assert "lib.desktop_agent.config.parse_connect_line" in cui, (
        "connect_ui must import the shared parser, not roll its own split")
    lau = _imports(ROOT / "desktop" / "launcher.py")
    assert "desktop.connect_ui.prompt_connect_line" in lau, (
        "launcher must delegate to the shared dialog, not re-implement it")


# ── NEUTER — both directions, because either side can drift ────────────

def test_NEUTER_changing_the_web_separator_is_caught():
    """Web side starts emitting a different shape → the app must reject it.

    This is the direction a parser-only test cannot see.
    """
    line = _web_emits(
        "https://tofu.example.com", "tok_N",
        neuter=lambda s: s.replace("return srv ? (srv + '  ' + token) : token;",
                                   "return srv ? (srv + '|' + token) : token;"))
    with pytest.raises(ValueError):
        _parse(line)


def test_NEUTER_tightening_the_app_parser_is_caught():
    """App side stops accepting what the web emits → real paste breaks.

    Reproduces a plausible 'cleanup' (split on a single space) and proves the
    contract test notices, rather than only the parser's own unit tests.
    """
    import lib.desktop_agent.config as cfg
    line = _web_emits("https://tofu.example.com", "tok_T")

    def _strict(s):
        parts = (s or "").split(" ")          # NOT .split() — no run collapsing
        if len(parts) != 2:
            raise ValueError("malformed")
        return parts[0].rstrip("/"), parts[1]

    original = cfg.parse_connect_line
    cfg.parse_connect_line = _strict
    try:
        with pytest.raises(ValueError):
            cfg.parse_connect_line(line)
    finally:
        cfg.parse_connect_line = original
    # And the real one still accepts it — proving the failure was the neuter.
    assert _parse(line)[1] == "tok_T"
