"""tests/test_autopilot_resume_after_crash.py — Resume armed autopilot on boot.

Covers ``lib.tasks_pkg.autopilot.resume_armed_autopilot_after_crash``: when the
server dies while an autopilot follow-up is mid-flight, startup recovery
restores the interrupted reply but the VU hook never finished (no follow-up, no
baton). This helper re-kicks the loop for every conversation that STILL carries
a persistent armed marker — and MUST leave un-armed conversations (clean
close-out / disarmed) alone.

CRITICAL invariant (the reason this was reworked): the DURABLE armed-marker is
the AUTHORITATIVE source, NOT the set of crash-recovered tasks. A conversation
armed from idle (marker present, but no in-flight/interrupted task at crash) is
absent from ``recovered_conv_ids`` yet MUST still resume — scanning markers
catches it; gating on recovered tasks would strand it.

Pure-unit: monkeypatches the marker probes + ``kick_autopilot`` so no DB, LLM,
or orchestrator runs. Includes an NC-bite proving the old recovered-only gating
misses the armed-but-idle conv.
"""

import pytest

import lib.message_queue as mq
import lib.tasks_pkg.autopilot as ap


def _patch(monkeypatch, armed_convs, marker_cfg=None, kicks_sink=None):
    """Wire list_armed_autopilot_convs / has_autopilot_marker /
    get_autopilot_marker_config / kick_autopilot.

    ``armed_convs`` — set of conv_ids that carry an armed marker (this is what
                      the DURABLE scan returns AND what has_autopilot_marker
                      answers True for).
    ``kicks_sink`` — list receiving (conv_id, cfg) for every kick that fired.
    """
    marker_cfg = marker_cfg or {}
    armed = set(armed_convs)
    monkeypatch.setattr(mq, 'list_armed_autopilot_convs',
                        lambda: sorted(armed))
    monkeypatch.setattr(mq, 'has_autopilot_marker', lambda cid: cid in armed)
    monkeypatch.setattr(mq, 'get_autopilot_marker_config',
                        lambda cid: dict(marker_cfg))

    def _fake_kick(conv_id, config=None):
        if kicks_sink is not None:
            kicks_sink.append((conv_id, config))
        return {'taskId': 'carrier-' + conv_id}

    monkeypatch.setattr(ap, 'kick_autopilot', _fake_kick)


def test_resumes_only_armed_convs(monkeypatch):
    """Marker scan drives resume; un-armed recovered convs are skipped."""
    kicks = []
    _patch(monkeypatch, armed_convs={'conv-armed'}, kicks_sink=kicks)

    # recovered set contains armed + two un-armed convs.
    resumed = ap.resume_armed_autopilot_after_crash(
        ['conv-armed', 'conv-clean', 'conv-disarmed'])

    assert resumed == ['conv-armed']
    assert [c for c, _ in kicks] == ['conv-armed']


def test_armed_but_idle_conv_resumes_even_when_not_recovered(monkeypatch):
    """★ The gap the rework closes: a conv armed from idle (marker present) had
    NO in-flight task at crash, so it is ABSENT from recovered_conv_ids — yet it
    MUST still be resumed because the marker scan is authoritative."""
    kicks = []
    _patch(monkeypatch, armed_convs={'conv-idle-armed'}, kicks_sink=kicks)

    # recovered_conv_ids does NOT contain the armed conv at all.
    resumed = ap.resume_armed_autopilot_after_crash(
        extra_conv_ids=['conv-some-other-recovered'])

    assert resumed == ['conv-idle-armed']
    assert [c for c, _ in kicks] == ['conv-idle-armed']


def test_resumes_with_no_recovered_ids_at_all(monkeypatch):
    """Called unconditionally at boot: even with an EMPTY recovery set, armed
    convs from the marker scan are resumed."""
    kicks = []
    _patch(monkeypatch, armed_convs={'conv-a', 'conv-b'}, kicks_sink=kicks)

    resumed = ap.resume_armed_autopilot_after_crash([])

    assert set(resumed) == {'conv-a', 'conv-b'}
    assert {c for c, _ in kicks} == {'conv-a', 'conv-b'}


def test_reuses_marker_config(monkeypatch):
    kicks = []
    _patch(monkeypatch, armed_convs={'conv-x'},
           marker_cfg={'model': 'claude-x', 'searchMode': 'off'},
           kicks_sink=kicks)

    ap.resume_armed_autopilot_after_crash(['conv-x'])

    assert len(kicks) == 1
    _cid, cfg = kicks[0]
    assert cfg['model'] == 'claude-x'
    assert cfg['searchMode'] == 'off'


def test_no_markers_is_noop(monkeypatch):
    kicks = []
    _patch(monkeypatch, armed_convs=set(), kicks_sink=kicks)
    assert ap.resume_armed_autopilot_after_crash(['conv-recovered']) == []
    assert kicks == []


def test_kick_refusal_not_counted_as_resumed(monkeypatch):
    """kick_autopilot returning taskId=None (e.g. task already running) →
    the conv is NOT reported resumed."""
    monkeypatch.setattr(mq, 'list_armed_autopilot_convs', lambda: ['conv-busy'])
    monkeypatch.setattr(mq, 'has_autopilot_marker', lambda cid: True)
    monkeypatch.setattr(mq, 'get_autopilot_marker_config', lambda cid: {})
    monkeypatch.setattr(ap, 'kick_autopilot',
                        lambda cid, cfg=None: {'taskId': None,
                                               'error': 'task_already_running'})
    assert ap.resume_armed_autopilot_after_crash([]) == []


def test_one_conv_failure_does_not_abort_batch(monkeypatch):
    """A raising kick for one conv must not prevent the others resuming."""
    monkeypatch.setattr(mq, 'list_armed_autopilot_convs',
                        lambda: ['conv-bad', 'conv-good'])
    monkeypatch.setattr(mq, 'has_autopilot_marker', lambda cid: True)
    monkeypatch.setattr(mq, 'get_autopilot_marker_config', lambda cid: {})

    def _kick(cid, cfg=None):
        if cid == 'conv-bad':
            raise RuntimeError('boom')
        return {'taskId': 'ok-' + cid}

    monkeypatch.setattr(ap, 'kick_autopilot', _kick)
    resumed = ap.resume_armed_autopilot_after_crash([])
    assert resumed == ['conv-good']


def test_NC_recovered_only_gating_misses_armed_idle(monkeypatch):
    """NC-bite: prove the OLD recovered-only gating strands the armed-but-idle
    conv. Simulate a resume that iterates ONLY recovered_conv_ids (the pre-fix
    behaviour) and assert it FAILS to resume an armed conv absent from that set
    — which the REAL marker-scan helper (test above) correctly resumes."""
    kicks = []
    armed = {'conv-idle-armed'}

    def _broken_recovered_only(recovered_ids):
        # The pre-fix logic: gate on the recovered set, not the marker scan.
        for cid in recovered_ids:
            if cid in armed:
                kicks.append(cid)
        return list(kicks)

    # armed conv is NOT in the recovered set → broken helper resumes nothing.
    resumed = _broken_recovered_only(['conv-other-recovered'])
    assert resumed == [], 'broken recovered-only gate wrongly resumed something'
    assert kicks == []

    # The REAL helper, on the SAME inputs, DOES resume it (marker scan wins).
    real_kicks = []
    _patch(monkeypatch, armed_convs=armed, kicks_sink=real_kicks)
    real_resumed = ap.resume_armed_autopilot_after_crash(
        extra_conv_ids=['conv-other-recovered'])
    assert real_resumed == ['conv-idle-armed']
    assert [c for c, _ in real_kicks] == ['conv-idle-armed']
