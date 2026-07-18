"""Frontend — the Status tab must refresh LIVE on a project-channel push frame,
not merely on panel-open / tab-select.

`_subscribePanelLive` subscribes to the `project` push channel while the panel
is open and debounce-refetches the live columns on every frame. Before the fix
it refreshed charter/board/influence/peers but OMITTED the Status tab, so the
"where are we" narrative sat stale while siblings finished epics — exactly the
"info isn't real-time" report. The handler now also calls `_refreshStatus(path)`
(which cascades into the nested watch lane via renderStatus → _refreshWatch).

Runs the REAL shipped `_subscribePanelLive` body under node with spies + a fake
pushSubscribe that captures the handler, fires a frame, lets the real 300ms
debounce elapse, and asserts every live-refresh fn — including `_refreshStatus`
— ran once. The biting NEUTER strips the `_refreshStatus(path);` line from the
debounced handler and proves the Status tab then goes stale on a push frame.
"""
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRAIN_JS = REPO / "static" / "js" / "project-brain.js"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(async\s+)?function %s\s*\(" % re.escape(name), src)
    assert m, f"{name} not found"
    i = src.index("{", m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


# The real _subscribePanelLive + _unsubscribePanelLive run against spies. A fake
# pushSubscribe captures the handler so we can fire a frame; the debounce uses
# node's real setTimeout, so we wait past 300ms before reading the spy log.
_HARNESS = r"""
'use strict';
const calls = [];
const _state = { path: '/proj/x', cbTimer: null, panelUnsub: null };
let _capturedHandler = null;
function pushSubscribe(channel, key, fn) { if (channel === 'project') _capturedHandler = fn; }
function pushUnsubscribe() {}
function _displayedProjectPath() { return '/proj/x'; }
function refreshCharter(p) { calls.push(['charter', p]); }
function refreshBoard(p) { calls.push(['board', p]); }
function refreshInfluence(p) { calls.push(['influence', p]); }
function _refreshPeers(p) { calls.push(['peers', p]); }
function _refreshStatus(p) { calls.push(['status', p]); }

__UNSUBSCRIBE__
__SUBSCRIBE__

_subscribePanelLive('/proj/x');
if (!_capturedHandler) { console.log(JSON.stringify({ error: 'no handler captured' })); process.exit(0); }
// A project-channel frame arrives (board claim / charter commit / epic done).
_capturedHandler({ type: 'activity', event: { seq: 1 } });
// Let the real 300ms debounce elapse, then read the spy log.
setTimeout(function () {
  console.log(JSON.stringify({ kinds: calls.map(c => c[0]), calls: calls }));
}, 400);
"""


def _run(subscribe_src, unsubscribe_src):
    script = (_HARNESS
              .replace("__SUBSCRIBE__", subscribe_src)
              .replace("__UNSUBSCRIBE__", unsubscribe_src))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    last = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(last)


def _src():
    txt = BRAIN_JS.read_text()
    return _extract_fn(txt, "_subscribePanelLive"), _extract_fn(txt, "_unsubscribePanelLive")


def test_push_frame_refreshes_status_and_all_live_columns():
    """A project-channel frame debounce-refreshes every live column, INCLUDING
    the Status tab (the fix) — each fn fires exactly once."""
    sub, unsub = _src()
    r = _run(sub, unsub)
    assert "error" not in r, r
    assert sorted(r["kinds"]) == sorted(
        ["charter", "board", "influence", "peers", "status"]), r
    for kind, path in r["calls"]:
        assert path == "/proj/x", r


def test_neuter_removing_refresh_status_goes_stale():
    """NEUTER: strip `_refreshStatus(path);` from the debounced handler → a push
    frame refreshes the other columns but NOT Status (the stale-status bug)."""
    sub, unsub = _src()
    neutered = re.sub(r"\n\s*_refreshStatus\(path\);", "", sub, count=1)
    assert neutered != sub and "_refreshStatus(path);" not in neutered, "neuter did not strip the call"
    r = _run(neutered, unsub)
    assert "error" not in r, r
    assert "status" not in r["kinds"], r
    # The other columns must still refresh — we only removed Status.
    assert sorted(r["kinds"]) == sorted(["charter", "board", "influence", "peers"]), r


if __name__ == "__main__":
    test_push_frame_refreshes_status_and_all_live_columns()
    print("PASS push_frame_refreshes_status_and_all_live_columns")
    test_neuter_removing_refresh_status_goes_stale()
    print("PASS neuter_removing_refresh_status_goes_stale")
    print("ALL GREEN")
