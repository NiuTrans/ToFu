#!/usr/bin/env python3
"""Guards for the MCP liveness probe after protocol revision 2026-07-28.

WHAT BROKE AND WHY IT HAD AN EXPIRY DATE
----------------------------------------
Tofu's keepalive used ``ClientSession.send_ping()`` as its health probe, and
treated ANY exception from it as "the transport is gone → reconnect". Protocol
revision 2026-07-28 removed ``ping`` from the schema entirely — measured
against the published schema.ts, ``ping`` occurs **0** times, in contrast with
``logging/setLevel`` which survives carrying an ``@deprecated`` tag and a
12-month window. Both SDK majors keep ``send_ping`` on the client for
compatibility with older servers, and the v2 low-level ``Server`` still
registers a default ``on_ping``, but neither binds a *server*: a conforming
2026-07-28 server answers ``-32601 Method not found``.

Measured end-to-end against a REAL mcp 2.0.0 server with ``ping`` dropped from
its handler table (the SDK's own documented opt-out)::

    list_tools      : OK n=1
    send_ping       : RAISED MCPError after 0.00s: Method not found
    list_tools AFTER: OK n=1     <-- transport ALIVE

Driving the real ``_keepalive_loop`` against exactly that, BEFORE the fix::

    pings attempted      : 12
    RECONNECTS triggered : 12    <-- storm, on a transport that never died
    cred probes run      : 0     <-- downstream of ping success ⇒ stalled forever

Both of those are the ticket's two acceptance criteria, and both are asserted
below.

THE ROOT FIX THESE GUARDS PIN
-----------------------------
Liveness is "did the peer ANSWER", not "did the probe SUCCEED". A JSON-RPC
error response proves the round trip completed. That verdict is correct for
every protocol revision, so it has no expiry date of its own — which is the
property the ping-based check lacked.

Everything here drives the SHIPPED ``MCPBridge._keepalive_loop`` /
``_probe_liveness`` and the SHIPPED classifiers in ``_errors``. No hand-written
re-implementation of the verdict: a second copy could agree with itself while
disagreeing with production, which is exactly how the original defect stayed
green.
"""
import asyncio
import os
import sys
import types
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip('mcp', reason='MCP SDK not installed')

pytestmark = pytest.mark.unit

from lib.mcp.client._errors import (  # noqa: E402
    _is_method_not_found,
    _is_peer_answered_error,
    _is_transport_dead_error,
)


# ── Fixtures that mimic the real wire shapes ──────────────────────────

def _method_not_found_exc():
    """Build the REAL SDK protocol error for -32601, whichever major is
    installed. Constructors differ (v1 takes ErrorData, v2 takes code+message)
    but both land the code on ``.error.code`` — measured on 1.27.2 and 2.0.0."""
    import mcp.types as t
    from mcp.shared import exceptions as _exc
    cls = getattr(_exc, 'McpError', None) or getattr(_exc, 'MCPError')
    code = getattr(t, 'METHOD_NOT_FOUND', -32601)
    try:
        return cls(t.ErrorData(code=code, message='Method not found'))
    except TypeError:
        return cls(code, 'Method not found')


class _Session:
    """Stand-in for ClientSession. ``supports`` picks which RPCs exist, and
    ``not_found`` picks which of them answer -32601 (i.e. are routed by the
    peer but absent from its method set)."""

    def __init__(self, supports=('send_ping', 'list_tools'), not_found=()):
        self._supports = set(supports)
        self._not_found = set(not_found)
        self.calls = []
        for m in ('send_ping', 'list_tools', 'send_discover', 'discover'):
            if m not in self._supports:
                setattr(self, m, None)

    async def _answer(self, meth):
        self.calls.append(meth)
        if meth in self._not_found:
            raise _method_not_found_exc()
        return types.SimpleNamespace(tools=[])

    async def send_ping(self):
        return await self._answer('send_ping')

    async def list_tools(self):
        return await self._answer('list_tools')

    async def send_discover(self, version):
        # Mirrors the REAL mcp 2.0.0 signature: send_discover(self, version)
        # REQUIRES an argument, while discover(self) does not. The first draft
        # of this fixture declared both as zero-arg, which hid a real defect:
        # the probe called send_discover() bare, got a TypeError, and read it
        # as "peer dead" -> reconnect. Only the end-to-end run against a real
        # v2 server exposed it. A fixture that is easier to satisfy than
        # production is how a guard certifies a broken build.
        return await self._answer('send_discover')

    async def discover(self):
        return await self._answer('discover')


class _DeadSession(_Session):
    """Transport genuinely gone — every RPC raises a dead-pipe error."""

    async def _answer(self, meth):
        self.calls.append(meth)
        raise ConnectionResetError('connection closed')


class _HangSession(_Session):
    """Peer never answers (no response at all) — must read as dead."""

    async def _answer(self, meth):
        self.calls.append(meth)
        await asyncio.sleep(30)


def _bridge(session, *, cred_due=True):
    """Wire a real MCPBridge around ``session`` with the reconnect and
    credential-probe side effects captured rather than executed."""
    from lib.mcp.client import _bridge as B
    br = B.MCPBridge()
    h = B._MCPServerHandle('srv', {'command': 'x'})
    h.session = session
    br._servers['srv'] = h
    br._configs['srv'] = {'command': 'x'}
    br._reconnects = []
    br._creds = []
    br._reconnect_server = lambda name: (br._reconnects.append(name), h)[1]
    br._run_cred_probe = lambda name: br._creds.append(name)
    br._cred_probe_spec = lambda name: {'tool': 't', 'args': {}}
    br._cred_probe_due = lambda name: cred_due
    return br, h


def _sweep(br, *, interval=0.02, window=0.35):
    """Run the SHIPPED _keepalive_loop for a fixed window and return sweeps."""
    from lib.mcp.client import _bridge as B
    orig_i, orig_p = B.MCP_KEEPALIVE_INTERVAL, B.MCP_PING_TIMEOUT
    B.MCP_KEEPALIVE_INTERVAL = interval
    B.MCP_PING_TIMEOUT = 1

    async def main():
        br._keepalive_stop = asyncio.Event()
        task = asyncio.get_running_loop().create_task(br._keepalive_loop())
        await asyncio.sleep(window)
        br._keepalive_stop.set()
        task.cancel()
        try:
            await task
        except BaseException:
            pass

    try:
        asyncio.run(main())
    finally:
        B.MCP_KEEPALIVE_INTERVAL, B.MCP_PING_TIMEOUT = orig_i, orig_p


# ── 1. The classifier: an error RESPONSE is proof of life ──────────────

class PeerAnsweredClassifierTest(unittest.TestCase):

    def test_method_not_found_is_recognised_by_its_numeric_code(self):
        """-32601 is owned by the JSON-RPC spec, so it cannot be renamed
        upstream the way ``McpError``→``MCPError`` was. Anchor there."""
        exc = _method_not_found_exc()
        self.assertEqual(getattr(exc.error, 'code', None), -32601,
                         'fixture did not build a real -32601 error')
        self.assertTrue(_is_method_not_found(exc))
        self.assertTrue(_is_peer_answered_error(exc))

    def test_the_code_not_the_wording_is_what_decides(self):
        """The discriminating case for the numeric anchor.

        Nothing obliges a server to phrase -32601 as the English "Method not
        found" — the JSON-RPC spec fixes the CODE, not the message, and a
        server may localise it or say "Unknown method". Matching on prose
        would silently stop recognising those, and the failure mode is the
        reconnect storm this batch exists to remove. Without this test the
        numeric lookup is indistinguishable from the text fallback (measured:
        removing the code branch left the suite fully green).
        """
        import mcp.types as t
        from mcp.shared import exceptions as _exc
        cls = getattr(_exc, 'McpError', None) or getattr(_exc, 'MCPError')
        code = getattr(t, 'METHOD_NOT_FOUND', -32601)
        for wording in ('Unknown method', '\u65b9\u6cd5\u672a\u627e\u5230', ''):
            try:
                exc = cls(t.ErrorData(code=code, message=wording))
            except TypeError:
                exc = cls(code, wording)
            self.assertTrue(
                _is_method_not_found(exc),
                f'-32601 carrying message {wording!r} was not recognised — '
                f'the classifier is reading the prose, not the code')
            self.assertTrue(_is_peer_answered_error(exc))

    def test_a_different_protocol_error_is_alive_but_not_method_not_found(self):
        """A -32602 (invalid params) is still an ANSWER (so: alive) but must
        not be mistaken for "this RPC is unimplemented", which would make the
        probe walk off a perfectly good method."""
        import mcp.types as t
        from mcp.shared import exceptions as _exc
        cls = getattr(_exc, 'McpError', None) or getattr(_exc, 'MCPError')
        code = getattr(t, 'INVALID_PARAMS', -32602)
        try:
            exc = cls(t.ErrorData(code=code, message='Invalid params'))
        except TypeError:
            exc = cls(code, 'Invalid params')
        self.assertTrue(_is_peer_answered_error(exc))
        self.assertFalse(_is_method_not_found(exc))

    def test_a_protocol_error_is_not_a_dead_transport(self):
        """The heart of the fix: these two verdicts must disagree on -32601."""
        exc = _method_not_found_exc()
        self.assertTrue(_is_peer_answered_error(exc))
        self.assertFalse(
            _is_transport_dead_error(exc),
            'a -32601 answer was classified as a dead transport — that is '
            'the misclassification that caused the reconnect storm')

    def test_a_dead_pipe_is_not_an_answer(self):
        """Complement: without this, everything reads as alive and a genuinely
        dead server would never be reconnected."""
        for exc in (ConnectionResetError('connection closed'),
                    BrokenPipeError('broken pipe')):
            self.assertFalse(_is_peer_answered_error(exc), repr(exc))
            self.assertTrue(_is_transport_dead_error(exc), repr(exc))

    def test_a_timeout_is_not_an_answer(self):
        """A timeout is the ABSENCE of a response. If it counted as liveness,
        a wedged server would be declared healthy forever."""
        self.assertFalse(_is_peer_answered_error(asyncio.TimeoutError()))
        self.assertFalse(_is_peer_answered_error(TimeoutError('timed out')))


# ── 2. Acceptance criterion #1: no reconnect storm ────────────────────

class NoReconnectStormTest(unittest.TestCase):

    def test_ping_answering_method_not_found_never_reconnects(self):
        """THE HEADLINE. A 2026-07-28 server answers -32601 to ping; the
        transport is provably fine (list_tools works). Zero reconnects."""
        s = _Session(supports=('send_ping', 'list_tools'),
                     not_found=('send_ping',))
        br, _ = _bridge(s)
        _sweep(br)
        self.assertGreater(len(s.calls), 0, 'no probe ran — window too short')
        self.assertEqual(
            br._reconnects, [],
            f'reconnect storm: {len(br._reconnects)} reconnects triggered on a '
            f'live transport whose ping merely answered -32601')

    def test_it_falls_back_to_a_probe_the_peer_implements(self):
        """Answering -32601 is a liveness proof AND a signal to move on, so the
        sweep must end up on an RPC this peer actually serves."""
        s = _Session(supports=('send_ping', 'list_tools'),
                     not_found=('send_ping',))
        br, _ = _bridge(s)
        _sweep(br)
        self.assertIn('list_tools', s.calls,
                      'never fell back past the unimplemented ping')
        self.assertEqual(br._probe_method.get('srv'), 'list_tools',
                         'resolved probe was not memoised')

    def test_the_memo_stops_repaying_the_fallback_walk(self):
        """Once resolved, later sweeps should not re-attempt the dead ping —
        otherwise every sweep pays a pointless round trip forever."""
        s = _Session(supports=('send_ping', 'list_tools'),
                     not_found=('send_ping',))
        br, _ = _bridge(s)
        _sweep(br, window=0.35)
        pings = s.calls.count('send_ping')
        lists = s.calls.count('list_tools')
        self.assertEqual(pings, 1,
                         f'ping was retried {pings}x despite answering -32601')
        self.assertGreater(lists, 1, 'memoised probe was not reused')

    def test_server_discover_is_preferred_when_the_session_offers_it(self):
        """``server/discover`` is the RPC servers MUST implement in
        2026-07-28 and means exactly "are you there + what can you do"."""
        s = _Session(supports=('discover', 'send_ping', 'list_tools'))
        br, _ = _bridge(s)
        _sweep(br, window=0.1)
        self.assertEqual(s.calls[0], 'discover',
                         f'probe order ignored server/discover: {s.calls[:3]}')
        self.assertEqual(br._reconnects, [])

    def test_a_probe_needing_arguments_is_skipped_not_called(self):
        """Regression for a defect this suite's own fixture originally hid.

        ``send_discover(version)`` requires an argument on mcp 2.0.0. Calling
        it bare raises TypeError, which is OUR error and says nothing about
        the peer — but the first implementation let it reach the verdict and
        reconnected a healthy server. Caught only by the end-to-end run.
        """
        s = _Session(supports=('send_discover', 'send_ping', 'list_tools'))
        br, _ = _bridge(s)
        _sweep(br, window=0.1)
        self.assertNotIn('send_discover', s.calls,
                         'called a probe that cannot be invoked zero-arg')
        self.assertEqual(br._reconnects, [],
                         'an un-callable probe was treated as a dead peer')
        self.assertEqual(br._probe_method.get('srv'), 'send_ping')

    def test_a_healthy_ping_still_short_circuits(self):
        """Regression floor: pre-2026-07-28 servers answer ping normally and
        must not start paying a heavier probe."""
        s = _Session(supports=('send_ping', 'list_tools'))
        br, _ = _bridge(s)
        _sweep(br, window=0.1)
        self.assertEqual(br._reconnects, [])
        self.assertNotIn('list_tools', s.calls,
                         'a healthy ping should not fall through to list_tools')


# ── 3. Complement: a genuinely dead server MUST still reconnect ───────

class DeadServerStillReconnectsTest(unittest.TestCase):
    """Without these, "never reconnect" would trivially satisfy the section
    above while destroying the feature the keepalive exists for."""

    def test_a_dead_pipe_triggers_reconnect(self):
        s = _DeadSession(supports=('send_ping', 'list_tools'))
        br, _ = _bridge(s)
        _sweep(br, window=0.1)
        self.assertGreater(len(br._reconnects), 0,
                           'a dead transport was NOT reconnected — the '
                           'keepalive lost its actual job')

    def test_a_peer_that_never_answers_triggers_reconnect(self):
        # The window MUST exceed interval + MCP_PING_TIMEOUT (0.02 + 1.0),
        # otherwise the sweep task is cancelled while still inside wait_for and
        # the probe never gets to time out — which reads as "treated as alive"
        # but is really the harness cutting the measurement short.
        s = _HangSession(supports=('send_ping', 'list_tools'))
        br, _ = _bridge(s)
        _sweep(br, interval=0.02, window=1.6)
        self.assertGreater(len(br._reconnects), 0,
                           'a hung peer was treated as alive')


# ── 4. Acceptance criterion #2: cred probe must not stall ─────────────

class CredentialProbeNotStalledTest(unittest.TestCase):

    def test_cred_probe_runs_when_ping_answers_method_not_found(self):
        """It used to hang off ping SUCCESS, so a 2026-07-28 server stalled it
        forever and an expired cookie would never surface. Measured before the
        fix: 0 probes across 12 sweeps."""
        s = _Session(supports=('send_ping', 'list_tools'),
                     not_found=('send_ping',))
        br, _ = _bridge(s, cred_due=True)
        _sweep(br)
        self.assertGreater(
            len(br._creds), 0,
            'credential probe never ran on a 2026-07-28 server — it is still '
            'gated on ping success')

    def test_cred_probe_does_not_run_when_the_server_is_dead(self):
        """Complement: probing credentials through a dead transport would
        classify a transport failure as a credential verdict."""
        s = _DeadSession(supports=('send_ping',))
        br, _ = _bridge(s, cred_due=True)
        _sweep(br, window=0.1)
        self.assertEqual(br._creds, [],
                         'credential probe ran against a dead transport')


# ── 5. Guard-the-guard: the fixture must really be a -32601 ───────────

class FixtureIntegrityTest(unittest.TestCase):
    """If the fixture silently stopped producing a real protocol error, every
    assertion above would pass for the wrong reason."""

    def test_fixture_is_a_real_sdk_protocol_error_carrying_the_code(self):
        exc = _method_not_found_exc()
        from lib.mcp.client._errors import _mcp_error_types
        sdk = _mcp_error_types()
        self.assertTrue(sdk, 'SDK protocol-error class not resolvable')
        self.assertIsInstance(exc, sdk)
        self.assertEqual(exc.error.code, -32601)

    def test_the_fixture_mirrors_the_real_sdk_arity(self):
        """Guard-the-guard: if the fixture drifts back to a zero-arg
        ``send_discover``, the arity defect above becomes untestable again.
        Asserted against the INSTALLED ClientSession, not a remembered fact.
        """
        import inspect
        from mcp import ClientSession
        real = getattr(ClientSession, 'send_discover', None)
        if real is None:
            self.skipTest('send_discover absent on this SDK major (v1)')
        required = [
            p.name for p in inspect.signature(real).parameters.values()
            if p.name != 'self' and p.default is inspect.Parameter.empty
            and p.kind is not inspect.Parameter.VAR_KEYWORD
            and p.kind is not inspect.Parameter.VAR_POSITIONAL
        ]
        self.assertTrue(
            required,
            'the real send_discover became zero-arg — re-check whether the '
            'arity skip is still needed')
        fixture = inspect.signature(_Session.send_discover)
        self.assertIn(
            'version', fixture.parameters,
            'fixture send_discover no longer mirrors the real required arg')

    def test_ping_is_absent_from_the_2026_07_28_schema(self):
        """The premise of this whole batch, asserted rather than assumed. Skips
        when the cached schema copy isn't present (it is a research artifact,
        not a build input)."""
        import glob
        hits = glob.glob(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'tool-results', '*', 'fetch_url_*.txt'))
        schema = None
        for p in hits:
            try:
                with open(p, encoding='utf-8', errors='replace') as fh:
                    head = fh.read(4000)
            except OSError:
                continue
            if 'schema/2026-07-28/schema.ts' in head:
                with open(p, encoding='utf-8', errors='replace') as fh:
                    schema = fh.read()
                break
        if schema is None:
            self.skipTest('cached 2026-07-28 schema.ts not available')
        self.assertIn('server/discover', schema,
                      'cached file is not the schema we think it is')
        self.assertNotIn('PingRequest', schema)
        self.assertEqual(
            schema.lower().count('ping'), 0,
            'ping reappeared in the 2026-07-28 schema — re-evaluate whether '
            'it is a legitimate probe again')


if __name__ == '__main__':
    unittest.main()
