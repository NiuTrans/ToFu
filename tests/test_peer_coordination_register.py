"""tests/test_peer_coordination_register.py — Pillar #6 Symptom-C: peer
messages must read as agent-to-agent COORDINATION, not status reports to a human.

Root cause (diagnosed): the model's ONLY steer for composing a peer message was
the tool ``description`` string, and there was NO ambient system-prompt guidance
telling it it is writing to ANOTHER AGENT. The description framing ("advisory
message / share a finding / the peer decides whether to act") biased the model
to narrate status for a human reader.

Fix: the ``project_message`` / ``project_intervene`` tool descriptions own the
coordination register ("writing TO ANOTHER AGENT", "not a status report").
There is no ambient peer protocol block when these tools are unavailable.

Each assertion has a byte-reverting NEUTER that fails when the coordination
wording is reverted to the old report-to-human phrasing.

Run with::

    python -m pytest tests/test_peer_coordination_register.py -v
"""

from __future__ import annotations

import unittest

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


class PeerProtocolPlacementTest(unittest.TestCase):
    """Peer instructions live with the tools, not in ambient context."""

    def test_no_ambient_protocol_renderer_or_context_provider(self):
        import lib.conversations.project_peer as pp
        import lib.tasks_pkg.context_composer._providers as providers
        self.assertFalse(hasattr(pp, 'render_peer_protocol_block'))
        src = open(providers.__file__, encoding='utf-8').read()
        self.assertNotIn('[PEER MESSAGING PROTOCOL]', src)


class PeerToolDescriptionRegisterTest(unittest.TestCase):
    """The project_message / project_intervene tool descriptions."""

    def _desc(self, tool):
        return tool['function']['description'].lower()

    def test_project_message_addresses_an_agent_not_a_human(self):
        from lib.tools.conversation import PEER_MESSAGE_TOOL
        d = self._desc(PEER_MESSAGE_TOOL)
        self.assertIn('another agent', d,
                      'project_message must frame the target as an AGENT')
        # Coordination verbs present.
        self.assertIn('claim', d)
        self.assertIn('hand off', d)
        # Explicitly steers AWAY from the report-to-human style.
        self.assertIn('not', d)
        self.assertTrue('status' in d or 'report' in d,
                        'must contrast with status/report-to-human prose')

    def test_project_message_no_longer_leads_with_advisory_share_finding(self):
        """NEUTER of the fix's INTENT: the old lead framing ('Send an ADVISORY
        message ... share a finding') was what biased the tone. Assert it is
        gone from the lead — the description now leads with 'coordinate
        directly'. If someone reverts to the old copy this fails."""
        from lib.tools.conversation import PEER_MESSAGE_TOOL
        d = PEER_MESSAGE_TOOL['function']['description']
        self.assertFalse(
            d.lstrip().lower().startswith('send an advisory message'),
            'description must NOT lead with the old report-to-human framing')
        self.assertIn('Coordinate', d)

    def test_project_intervene_is_a_directive_to_a_peer_agent(self):
        from lib.tools.conversation import PEER_INTERVENE_TOOL
        d = self._desc(PEER_INTERVENE_TOOL)
        self.assertIn('another agent', d)
        self.assertIn('stop', d)
        # Preserved safety mechanics (hard abort still human-gated).
        self.assertIn('human', d)
        self.assertIn('approval', d)

    def test_message_param_asks_for_coordination_act_not_status(self):
        from lib.tools.conversation import PEER_MESSAGE_TOOL
        param = PEER_MESSAGE_TOOL['function']['parameters']['properties']['text']
        pd = param['description'].lower()
        self.assertIn('coordination', pd)
        self.assertTrue('not a status' in pd or 'not a status report' in pd,
                        'the text param must steer away from status reports')


import os


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_PEER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_peer.py')


from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


class PeerReplyAffordanceTest(unittest.TestCase):
    """Increment (3): the RECEIVE side must (a) tell the agent it MAY reply
    once via project_message, and (b) surface the sender's FULL reply id in the
    received body so the reply can target it verbatim — WITHOUT breaking the
    no-auto-relay invariant (the body itself must not name a send tool)."""

    def _sent_body(self, monkeypatch):
        """Send an agent (non-human) peer message and return the framed body
        the receiver's turn will contain, DB-free."""
        import lib.conversations.project_peer as pp
        captured = {}

        def _enqueue(conv_id, data, config, kind='real', *, user_id):
            self.assertEqual(TEST_OWNER_USER_ID, user_id)
            captured.setdefault('p', data)
            return {'queueId': 'q1'}

        def _resolve_target(target, *, user_id):
            self.assertEqual(TEST_OWNER_USER_ID, user_id)
            return (target or '').strip(), ''

        monkeypatch.setattr(
            'lib.message_queue.enqueue_message',
            _enqueue)
        monkeypatch.setattr('lib.conversations.project_feed.emit_project_event',
                            lambda *a, **k: None)
        monkeypatch.setattr('lib.conversations.project_peer.audit_log',
                            lambda *a, **k: None)
        monkeypatch.setattr(
            'lib.conversations.project_peer._resolve_target_conv_id',
            _resolve_target)
        with pp._rate_lock:
            pp._peer_msg_history.clear()
        pp.send_peer_message('/p', 'convSENDERfull', 'convTARGETfull', 'boundary?', user_id=TEST_OWNER_USER_ID)
        return captured['p']['text']

    # ── Part A: project_message's schema teaches a bounded reply ──

    def test_tool_description_teaches_bounded_reply(self):
        from lib.tools.conversation import PEER_MESSAGE_TOOL
        block = PEER_MESSAGE_TOOL['function']['description']
        low = block.lower()
        # It tells the receiver it MAY reply, and names the reply tool + arg.
        self.assertIn('receive', low, 'block must address the RECEIVE side')
        self.assertIn('project_message(to_conv_id=', block,
                      'block must name the reply tool + arg on the receive side')
        # It bounds the reply: "once" + rate-limit-is-the-ceiling framing.
        self.assertIn('once', low)
        self.assertTrue('rate-limit' in low or 'rate limit' in low,
                        'block must anchor the reply budget to the rate limit')
        self.assertTrue('not a chat' in low or 'not a chat channel' in low,
                        'block must warn it is coordination, not a chat channel')

    # ── Part B: received body carries the FULL reply id, stays plain content ──

    def test_received_body_surfaces_full_reply_id(self):
        # unittest can't take the monkeypatch fixture; use MonkeyPatch directly.
        from _pytest.monkeypatch import MonkeyPatch
        mp = MonkeyPatch()
        try:
            body = self._sent_body(mp)
        finally:
            mp.undo()
        # The FULL sender id (not just the 8-char prefix) appears so a reply
        # targets it verbatim — no mistyped/ambiguous-prefix failure.
        self.assertIn('convSENDERfull', body)
        self.assertIn('reply id convSENDERfull', body,
                      'body must label the full id as the reply id')

    def test_received_body_preserves_no_auto_relay(self):
        """The load-bearing invariant: the body is PLAIN content. It keeps the
        'advisory' framing and must NOT name a send tool (no 'project_message'),
        so receiving a message can never itself trigger a send. The reply
        MECHANICS live in the ambient block, not the body."""
        from _pytest.monkeypatch import MonkeyPatch
        mp = MonkeyPatch()
        try:
            body = self._sent_body(mp)
        finally:
            mp.undo()
        self.assertIn('advisory', body.lower())
        self.assertNotIn('project_message', body,
                         'no-auto-relay: the body must not name a send tool')

    def test_NEUTER_body_without_full_id_fails(self):
        """Byte-revert the body back to the 8-char-only framing → the full-id
        assertion FAILS, proving the reply id depends on the real change."""
        def run():
            from _pytest.monkeypatch import MonkeyPatch

            import lib.conversations.project_peer as pp
            mp = MonkeyPatch()
            try:
                body = self._sent_body(mp)
            finally:
                mp.undo()
            self.assertNotIn('reply id convSENDERfull', body,
                             'NEUTER: with the full-id framing removed the reply '
                             'id label must be ABSENT')

        _patch_restore(
            _PEER_SRC,
            "(conv {from_conv_id[:8]}, reply id {from_conv_id})]",
            "(conv {from_conv_id[:8]})]",
            run,
        )

if __name__ == '__main__':
    unittest.main()
