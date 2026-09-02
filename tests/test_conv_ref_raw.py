"""tests/test_conv_ref_raw.py — raw-mode get_conversation.

Pins the debugging surface of ``get_conversation(raw=True)``: it must emit the
full authority snapshot — row-level metadata (created_at, updated_at,
msg_count, rev), the raw settings, and EVERY field of every message
    (settlement, usage, model, _turnId, toolRounds, …) — as structured JSON
dump, instead of the lossy prose transcript. Also pins that the readable
transcript still works and that backend-agnostic JSON coercion tolerates an
already-decoded (PG-style) messages value.

Fixtures enter through the bounded conversation import operation used for
pre-turn archives; production reads still derive turn-native transcripts.
"""
from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/bar.py', 'lib/foo.py'}

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.conv_ref import build_conversation_digest, get_conversation
from lib.conv_ref._detail import _coerce_json

pytest_plugins = ('tests._chat_sidecar',)


@pytest.mark.api
class TestGetConversationRaw:
    @pytest.fixture(autouse=True)
    def seed(self, flask_client, chat_sidecar):
        from tests._seed import seed_conversation
        now = int(time.time() * 1000)
        tag = f'{now}'
        self.cid = f'cvraw-{tag}'
        self.messages = [
            {'role': 'user', 'content': 'hello there',
             'timestamp': now},
            {'role': 'assistant', 'content': 'hi!',
             'model': 'test-model-x', 'finishReason': 'stop',
             'usage': {'input_tokens': 10, 'output_tokens': 3},
             'modifiedFileList': ['lib/foo.py'],
             'toolRounds': [{'toolName': 'read_files', 'status': 'done',
                             'args': {'path': 'lib/foo.py'}}]},
        ]
        seed_conversation(
            self.cid, title='Raw Test', messages=self.messages,
            created_at=now, updated_at=now + 5,
            settings={'preset': 'sonnet', 'projectPath': '/tmp/x'})
        yield

    @staticmethod
    def _seed_conversation(cid, *, title, messages, settings=None,
                           created_at=None, updated_at=None):
        from tests._seed import seed_conversation
        return seed_conversation(
            cid, title=title, messages=messages, settings=settings or {},
            created_at=created_at, updated_at=updated_at)

    def test_raw_dump_preserves_message_metadata(self):
        out = get_conversation(self.cid, raw=True, user_id=1)
        assert 'Raw Conversation Record' in out
        # The metadata fields the prose transcript drops must all appear.
        for token in ('providerFinishReason', 'test-model-x', '_turnId', 'usage',
                      'modifiedFileList', 'toolRounds', 'input_tokens'):
            assert token in out, f'missing {token!r} in raw dump'
        # Row-level columns must appear too.
        assert 'msg_count' in out and 'rev' in out and 'updated_at' in out
        # It's a fenced JSON block that round-trips.
        body = out.split('```json', 1)[1].rsplit('```', 1)[0]
        record = json.loads(body)
        assert record['id'] == self.cid
        assert record['msg_count'] == 2
        assert record['settings']['preset'] == 'sonnet'
        assert record['messages'][1]['_turnSettlement'][
            'providerFinishReason'
        ] == 'stop'

    def test_prose_transcript_still_default(self):
        out = get_conversation(self.cid, user_id=1)
        assert 'Referenced Conversation' in out
        assert 'hello there' in out
        # Prose mode does NOT dump the raw metadata keys verbatim.
        assert 'finishReason' not in out

    def test_coerce_json_tolerates_decoded_value(self):
        # PG driver could hand back an already-decoded list/dict — the fallback.
        already = [{'role': 'user', 'content': 'x'}]
        assert _coerce_json(already, default=[]) is already
        assert _coerce_json('{"a": 1}', default={}) == {'a': 1}
        assert _coerce_json(None, default=[]) == []

    def test_raw_missing_conversation(self):
        out = get_conversation(
            'does-not-exist-xyz', raw=True, user_id=1)
        assert 'not found' in out.lower()

    def test_build_digest_structure(self):
        d = build_conversation_digest(self.cid, user_id=1)
        assert d is not None
        assert d['convId'] == self.cid
        assert d['title'] == 'Raw Test'
        assert d['preset'] == 'sonnet'
        assert d['msgCount'] == 2
        assert len(d['messages']) == 2
        # Row-level timestamps are now carried (the "multi-query DB" ask).
        assert d['createdAt'] > 0 and d['updatedAt'] > 0
        assert d['updatedAt'] >= d['createdAt']
        assert d['truncated'] is False and d['omitted'] == 0
        # user row: text preview, per-message timestamp, no tools
        u = d['messages'][0]
        assert u['role'] == 'user' and 'hello there' in u['text']
        assert u['ts'] > 0  # message-level timestamp surfaced
        assert 'tools' not in u
        # assistant row: tools are now rich descriptors (name + arg + status),
        # NOT bare tool-name strings.
        a = d['messages'][1]
        assert a['role'] == 'assistant'
        assert isinstance(a['tools'], list) and len(a['tools']) == 1
        td = a['tools'][0]
        assert td['name'] == 'read_files'
        assert td['arg'] == 'lib/foo.py'   # primary arg extracted from args.path
        assert td['status'] == 'done'

    def test_build_digest_raw_carries_metadata(self):
        # raw=True → dict marked raw + top-level rev + per-message low-level
        # metadata (model / usage / finishReason / turnId) on the assistant row.
        d = build_conversation_digest(self.cid, raw=True, user_id=1)
        assert d is not None
        assert d.get('raw') is True
        assert d.get('rev') is not None and isinstance(d['rev'], int)
        a = d['messages'][1]  # the assistant message
        assert a['model'] == 'test-model-x'
        assert a['finishReason'] == 'stop'
        assert isinstance(a['turnId'], str) and a['turnId']
        assert a['usage'] == {'in': 10, 'out': 3}
        # The user row carries none of the assistant-only fields.
        u = d['messages'][0]
        assert 'model' not in u and 'finishReason' not in u and 'usage' not in u

    def test_build_digest_default_omits_raw_metadata(self):
        # raw=False (default) → NONE of the raw markers/metadata appear
        # (byte-identical to the prior digest shape).
        d = build_conversation_digest(self.cid, user_id=1)
        assert d is not None
        assert 'raw' not in d
        assert 'rev' not in d
        for row in d['messages']:
            assert 'model' not in row
            assert 'usage' not in row
            assert 'finishReason' not in row
            assert 'turnId' not in row

    def _capture_post_build_meta(self, fn_args):
        """Drive the REAL _handle_conv_ref_tool _post_build closure without a
        live executor. Stubs ``simple_call`` in the _brain module to capture the
        ``post_build`` callback (mirrors test_mcp_tool_links.PostBuildTitleTest),
        then invokes it against a fresh ``meta`` dict and returns that meta."""
        import lib.tasks_pkg.handlers.misc._brain as brain

        captured = {}

        def _fake_simple_call(task, fn, args, rn, round_entry, tc_id,
                              *, executor, source, module_tag='', title='',
                              post_build=None, **_kw):
            captured['post_build'] = post_build
            return tc_id, 'ok', False

        orig = brain.simple_call
        brain.simple_call = _fake_simple_call
        try:
            brain._handle_conv_ref_tool(
                {'convId': None, '_userId': 1}, {},
                'get_conversation', 't', fn_args,
                1, {}, {}, '/tmp/x', False,
            )
            meta = {}
            captured['post_build'](meta, 'RAW JSON DUMP', fn_args)
            return meta
        finally:
            brain.simple_call = orig

    def test_handler_attaches_digest_in_raw_mode(self):
        # THE RAW-MODE FIX (2026-07-23): a get_conversation(raw=True) read used
        # to SKIP the digest card (the `_fn_args.get('raw')` short-circuit), so
        # the human saw the ugly 78KB JSON blob truncated by L0. The handler
        # must now attach `convDigest` for raw reads too (rebuilt off the DB).
        meta_raw = self._capture_post_build_meta(
            {'conversation_id': self.cid, 'raw': True})
        assert 'convDigest' in meta_raw, 'raw-mode read must still attach the card'
        assert meta_raw['convDigest']['convId'] == self.cid
        assert meta_raw['convDigest']['msgCount'] == 2
        # …and the raw flag propagates through the handler so the card renders
        # the RAW badge + per-message metadata (the fix this turn).
        assert meta_raw['convDigest'].get('raw') is True
        # …and the TOOL-SURFACE DEFAULT is now raw too (owner-directed
        # 2026-07-28): a bare call reads like a DB query, so its card carries
        # the RAW badge. Pinned in tests/test_conv_ref_raw_default.py.
        meta_bare = self._capture_post_build_meta(
            {'conversation_id': self.cid})
        assert 'convDigest' in meta_bare
        assert meta_bare['convDigest'].get('raw') is True
        # …and an EXPLICIT opt-out still attaches a plain, non-raw card — the
        # protection this assertion has always carried: the badge must not
        # claim a debug read that did not happen.
        meta_default = self._capture_post_build_meta(
            {'conversation_id': self.cid, 'raw': False})
        assert 'convDigest' in meta_default
        assert meta_default['convDigest']['convId'] == self.cid
        assert 'raw' not in meta_default['convDigest']

    def test_build_digest_self_reference_is_none(self):
        # Digesting the CURRENT conversation is a no-op (caller falls back).
        assert build_conversation_digest(
            self.cid, current_conv_id=self.cid, user_id=1) is None

    def test_build_digest_missing_is_none(self):
        assert build_conversation_digest(
            'does-not-exist-xyz', user_id=1) is None

    def _seed_empty(self, cid):
        """Seed an existing-but-EMPTY conversation row (title + settings, no
        messages) — the shape from the reported screenshot (msg_count 0,
        non-zero rev)."""
        now = int(time.time() * 1000)
        self._seed_conversation(
            cid, title='Empty Conv', messages=[], created_at=now,
            updated_at=now, settings={'preset': 'sonnet'})

    def test_build_digest_empty_conversation_still_gets_a_card(self, flask_client):
        # THE REPORTED BUG: an existing-but-empty conversation used to return
        # None here, so no convDigest meta was attached and the frontend fell
        # back to dumping the raw ═══ header + JSON skeleton as Markdown (two
        # giant bars + a JSON blob). The digest must exist with messages: []
        # so the card renders its designed empty state.
        cid = f'cvempty-{int(time.time() * 1000)}'
        self._seed_empty(cid)
        d = build_conversation_digest(cid, user_id=1)
        assert d is not None, 'empty conversation forfeited its card'
        assert d['convId'] == cid and d['title'] == 'Empty Conv'
        assert d['preset'] == 'sonnet'
        assert d['msgCount'] == 0 and d['messages'] == []
        assert d['truncated'] is False and d['omitted'] == 0
        assert d['trailingDropped'] == 0
        # Raw mode still marks the card RAW and carries the row revision.
        dr = build_conversation_digest(cid, raw=True, user_id=1)
        assert dr is not None and dr.get('raw') is True
        assert isinstance(dr.get('rev'), int)
        assert dr['messages'] == []

    def test_raw_dump_empty_conversation_is_concise(self, flask_client):
        # The model does not need a pretty-printed JSON skeleton of an EMPTY
        # record — a raw read of a 0-message conversation answers in one
        # line that still carries every fact worth having.
        cid = f'cvemptyraw-{int(time.time() * 1000)}'
        self._seed_empty(cid)
        out = get_conversation(cid, raw=True, user_id=1)
        assert '```json' not in out
        assert 'Raw Conversation Record' not in out
        assert 'no messages' in out.lower()
        assert cid in out and 'Empty Conv' in out
        assert len(out) < 400

    def test_prose_empty_conversation_is_concise(self, flask_client):
        # The prose path already had a one-liner; pin it so the two modes
        # can't drift apart.
        cid = f'cvemptyprose-{int(time.time() * 1000)}'
        self._seed_empty(cid)
        out = get_conversation(cid, user_id=1)
        assert 'no messages' in out.lower() and '```json' not in out
        assert len(out) < 400

    def test_build_digest_long_preview(self, flask_client):
        # A message longer than the 750-char preview (B-widening 2026-07-23)
        # must be previewed to ~750 and carry an (expandable) `full` longer
        # than the preview.
        from lib.conv_ref._detail import DIGEST_PREVIEW
        now = int(time.time() * 1000)
        cid = f'cvlong-{now}'
        body = 'word ' * 400  # ~2000 chars
        msgs = [{'role': 'user', 'content': body, 'timestamp': now}]
        self._seed_conversation(
            cid, title='Long', messages=msgs, created_at=now, updated_at=now)
        d = build_conversation_digest(cid, user_id=1)
        row = d['messages'][0]
        # Preview is bounded to the DEFAULT preview length (+ ellipsis),
        # full is much longer. Assert against the constant so it can't drift.
        assert DIGEST_PREVIEW == 750
        assert (DIGEST_PREVIEW - 20) <= len(row['text']) <= (DIGEST_PREVIEW + 2)
        assert row['text'].endswith('…')
        assert 'full' in row and len(row['full']) > len(row['text'])

    def test_build_digest_head_tail_omission(self, flask_client):
        # A conversation longer than head+tail keeps the first `head` and last
        # `tail`, drops the middle, and inserts a single {omitted: X} marker
        # between the head slice and the tail slice.
        now = int(time.time() * 1000)
        cid = f'cvht-{now}'
        # 20 messages; digest head=3, tail=5 → 12 omitted.
        msgs = [{'role': 'user' if i % 2 == 0 else 'assistant',
                 'content': f'msg-{i}', 'timestamp': now + i} for i in range(20)]
        self._seed_conversation(
            cid, title='HT', messages=msgs, created_at=now,
            updated_at=now + 100)
        d = build_conversation_digest(
            cid, head=3, tail=5, user_id=1)
        assert d['msgCount'] == 20
        assert d['truncated'] is True and d['omitted'] == 12
        content_rows = [m for m in d['messages'] if 'omitted' not in m]
        marker_rows = [m for m in d['messages'] if 'omitted' in m]
        # Exactly one omission marker, carrying the omitted count.
        assert len(marker_rows) == 1 and marker_rows[0]['omitted'] == 12
        # 3 head + 5 tail content rows, original indices preserved.
        assert len(content_rows) == 8
        assert [m['index'] for m in content_rows[:3]] == [1, 2, 3]
        assert [m['index'] for m in content_rows[-5:]] == [16, 17, 18, 19, 20]
        # The head slice must contain msg-0 and the tail slice msg-19.
        assert 'msg-0' in content_rows[0]['text']
        assert 'msg-19' in content_rows[-1]['text']

    def test_build_digest_empty_content_tool_round_fallback(self, flask_client):
        # An assistant round with EMPTY content but toolRounds/thinking must NOT
        # render as blank — it falls back to thinking, else a tool summary, and
        # is flagged textFallback so the frontend styles it as a summary.
        now = int(time.time() * 1000)
        cid = f'cvfb-{now}'
        msgs = [
            {'role': 'user', 'content': 'do the thing', 'timestamp': now},
            # empty content, only toolRounds → tool summary fallback
            {'role': 'assistant', 'content': '', 'timestamp': now + 1,
             'toolRounds': [
                 {'toolName': 'read_files', 'status': 'done',
                  'args': {'path': 'lib/bar.py'}},
                 {'toolName': 'grep_search', 'status': 'done',
                  'args': {'pattern': 'needle'}}]},
            # empty content, but has thinking → thinking wins over tool summary
            {'role': 'assistant', 'content': '', 'timestamp': now + 2,
             'thinking': 'Considering the approach carefully',
             'toolRounds': [{'toolName': 'list_dir', 'status': 'done',
                             'args': {'path': '.'}}]},
        ]
        self._seed_conversation(
            cid, title='FB', messages=msgs, created_at=now,
            updated_at=now + 10)
        d = build_conversation_digest(cid, user_id=1)
        tool_only = d['messages'][1]
        # Tool-summary fallback (no thinking): text carries the tool names,
        # flagged textFallback, NOT an empty "(no text)".
        assert tool_only['text']
        assert 'read_files' in tool_only['text'] and 'lib/bar.py' in tool_only['text']
        assert tool_only.get('textFallback') is True
        # Thinking beats tool summary.
        think_row = d['messages'][2]
        assert 'Considering the approach' in think_row['text']
        assert think_row.get('textFallback') is True

    def test_build_digest_tail_anchors_last_content(self, flask_client):
        # THE CORE FIX: a conversation ending in a run of tool-only (empty
        # content) assistant rounds must anchor its tail on the LAST
        # content-bearing message (the conclusion), dropping the trailing
        # tool-only rounds — so the digest's last row is the real conclusion,
        # not a blank tool round.
        now = int(time.time() * 1000)
        cid = f'cvtail-{now}'
        msgs = [{'role': 'user', 'content': f'q{i}', 'timestamp': now + i}
                for i in range(4)]
        # The real conclusion — index 5 (1-based) — with substantive content.
        msgs.append({'role': 'assistant', 'content': 'THE FINAL ANSWER is 42',
                     'timestamp': now + 100})
        # …followed by 3 trailing tool-only rounds (empty content).
        for j in range(3):
            msgs.append({'role': 'assistant', 'content': '', 'timestamp': now + 200 + j,
                         'toolRounds': [{'toolName': 'run_command', 'status': 'done',
                                         'args': {'command': f'cleanup {j}'}}]})
        self._seed_conversation(
            cid, title='Tail', messages=msgs, created_at=now,
            updated_at=now + 300)
        d = build_conversation_digest(
            cid, head=2, tail=2, user_id=1)
        content_rows = [m for m in d['messages'] if 'omitted' not in m]
        # The LAST content row must be the real conclusion (index 5), not a
        # trailing tool-only round (index 6/7/8).
        last = content_rows[-1]
        assert last['index'] == 5
        assert 'THE FINAL ANSWER' in last['text']
        # The 3 trailing tool-only rounds were dropped.
        assert d['trailingDropped'] == 3
        assert d['truncated'] is True
        # No dropped tool-only round leaked into the rows.
        assert all(m.get('index') != 8 for m in content_rows)
