"""Guard the OOMWATCH boot post-mortem verdict logic (liantong_kb.memwatch).

The post-mortem compares the cgroup's cumulative ``oom_kill`` / ``failcnt``
counters against the previous boot and logs a verdict about WHY the previous
process died. The trap it must not fall into: ``oom_kill`` is a *shared,
hierarchy-wide, cumulative* counter — in a shared pod it counts neighbours'
OOM kills too. So ``d_oom > 0`` on its own does NOT prove THIS server was the
OOM victim. Only ``failcnt`` growth means *this* cgroup hit its own memory
limit. The confident "your server was very likely OOM-killed" claim must be
gated on that self-attributing signal, not on the shared counter alone.

These tests pin the four verdict tiers and, in particular, that the
neighbour-kill case (d_oom>0, failcnt +0 — the real 2026-07-11 incident) is
NOT reported as a confident OOM kill of this server.
"""

import json
import logging

import pytest

memwatch = pytest.importorskip('liantong_kb.memwatch')

pytestmark = pytest.mark.unit

_LIMIT = 200 * 1024 ** 3


def _run_post_mortem(tmp_path, monkeypatch, *, prev, cur, peak=None):
    """Seed the prev-boot state file, stub _cgroup_mem to `cur`, run post-mortem.

    prev/cur are (oom_kill, failcnt). Returns the captured WARNING/INFO records.
    """
    state = tmp_path / '.memwatch_state.json'
    if prev is not None:
        state.write_text(json.dumps({'oom_kill': prev[0], 'failcnt': prev[1],
                                     'ts': 0, 'pid': 1}))
    monkeypatch.setattr(memwatch, '_state_path', lambda: str(state))
    oom_kill, failcnt = cur
    usage = _LIMIT
    monkeypatch.setattr(memwatch, '_cgroup_mem',
                        lambda: (usage, _LIMIT, peak or _LIMIT, oom_kill, failcnt))
    return state


def _post_mortem_text(caplog):
    return '\n'.join(r.getMessage() for r in caplog.records
                     if 'OOMWATCH' in r.getMessage())


def test_neighbour_kill_not_reported_as_our_oom(tmp_path, monkeypatch, caplog):
    """d_oom>0 but failcnt +0 = a NEIGHBOUR was killed, not us.

    This is the exact 2026-07-11 false positive: the shared oom_kill counter
    grew, but this cgroup never hit its own limit (failcnt=0) and our RSS was
    ~1% of the pod. The verdict MUST NOT claim our death was 'very likely an
    OOM kill' — it must attribute the kill to a neighbour and point elsewhere.
    """
    _run_post_mortem(tmp_path, monkeypatch, prev=(560, 0), cur=(566, 0))
    with caplog.at_level(logging.INFO, logger='server.memwatch'):
        memwatch._post_mortem()
    text = _post_mortem_text(caplog).lower()
    assert 'oom killer fired' in text          # it DID observe the kills
    assert 'very likely an oom kill' not in text  # …but NOT blamed on us
    assert 'neighbour' in text                  # correctly attributed elsewhere


def test_genuine_saturation_is_reported_confidently(tmp_path, monkeypatch, caplog):
    """d_oom>0 AND failcnt grew = this cgroup hit its OWN limit and killed —
    genuine saturation; a confident capacity verdict is warranted."""
    _run_post_mortem(tmp_path, monkeypatch, prev=(560, 10), cur=(566, 14))
    with caplog.at_level(logging.INFO, logger='server.memwatch'):
        memwatch._post_mortem()
    text = _post_mortem_text(caplog).lower()
    assert 'oom killer fired' in text
    assert 'saturation' in text or 'plausible oom victim' in text
    assert 'neighbour' not in text


def test_limit_hit_without_kill(tmp_path, monkeypatch, caplog):
    """failcnt grew but no OOM kill: allocation stalls, no kill recorded."""
    _run_post_mortem(tmp_path, monkeypatch, prev=(566, 10), cur=(566, 12))
    with caplog.at_level(logging.INFO, logger='server.memwatch'):
        memwatch._post_mortem()
    text = _post_mortem_text(caplog).lower()
    assert 'hit its memory limit' in text
    assert 'very likely an oom kill' not in text


def test_no_events_is_quiet(tmp_path, monkeypatch, caplog):
    """No counter movement = clean previous exit; only an INFO breadcrumb."""
    _run_post_mortem(tmp_path, monkeypatch, prev=(566, 0), cur=(566, 0))
    with caplog.at_level(logging.INFO, logger='server.memwatch'):
        memwatch._post_mortem()
    text = _post_mortem_text(caplog).lower()
    assert 'no cgroup oom/limit events' in text


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
