"""``get_conversation`` the TOOL defaults to the raw DB record.

Owner-directed (2026-07-28): a model reading a past conversation is almost
always debugging, and the prose transcript SUMMARIZES tool rounds and drops
per-message metadata — so the interesting fields were absent by default and
the model had to know to ask for ``raw=true`` to get them. The tool surface now
behaves like querying the database: omit the parameter and you get everything.

What these tests pin, and why each is a RESULT rather than a constant:

* **The executor's behaviour, not its default expression.** Asserting
  ``fn_args.get('raw', True)`` appears in the source would keep passing if the
  call were rewired to a different resolver; asserting the returned STRING is
  the raw record holds across any rewrite.
* **The library default is UNCHANGED.** ``lib.conv_ref.get_conversation`` is
  also called by the ``@``-mention injection path
  (``lib/chat/messages.py::resolve_conv_refs``) and by the human export route
  (``routes/conversations.py::export_conv``), which both want prose. Flipping
  the tool surface must not flip those — so the complement is pinned too, and
  it is what makes this a scoped change rather than a global one.
* **The card and the payload agree.** The digest badge is resolved
  independently in ``lib/tasks_pkg/handlers/misc/_brain.py``; when each side
  read ``fn_args`` for itself, a one-sided flip would render a card labelled
  ``RAW · debug`` next to a prose payload (or the reverse). Both now resolve
  through :func:`lib.conv_ref.raw_requested`, and the test drives BOTH real
  code paths off one args dict and compares them.
* **The opt-out really opts out.** A default that cannot be turned off is a
  removed feature, not a changed default.
"""

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/foo.py'}

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


MESSAGES = [
    {'role': 'user', 'content': 'why did the cache break', '_msgId': 'u-1',
     'timestamp': 1700000000000},
    {'role': 'assistant', 'content': 'the prefix moved', '_msgId': 'a-1',
     'model': 'test-model-x', 'finishReason': 'stop',
     'usage': {'input_tokens': 11, 'output_tokens': 4},
     'modifiedFileList': ['lib/foo.py'],
     'toolRounds': [{'toolName': 'read_files', 'status': 'done',
                     'args': {'path': 'lib/foo.py'}}]},
]


class _Row(dict):
    """dict that also answers ``row['col']`` like the DB wrapper."""


@pytest.fixture
def fake_row(monkeypatch):
    """Install a single fake conversation row for both conv_ref read paths."""
    from lib.conv_ref import _detail
    row = _Row({
        'id': 'c1', 'user_id': 1, 'title': 'Cache bug',
        'messages': json.dumps(MESSAGES), 'created_at': 1, 'updated_at': 2,
        'settings': json.dumps({'preset': 'sonnet'}),
        'msg_count': len(MESSAGES), 'rev': 3,
    })

    class _Cur:
        def fetchone(self):
            return row

    class _DB:
        def execute(self, sql, params=()):
            return _Cur()

    monkeypatch.setattr(_detail, '_get_db', lambda: _DB())
    return row


def _run_tool(fn_args):
    """Drive the REAL tool executor (not the library function directly)."""
    from lib.conv_ref import execute_conv_ref_tool
    return execute_conv_ref_tool('get_conversation', fn_args)


def _is_raw(out):
    """A raw read is the JSON record; prose is the ``═══`` transcript."""
    return 'Raw Conversation Record' in out and '```json' in out


class TestToolDefaultsToRaw:
    def test_omitting_the_parameter_yields_the_raw_record(self, fake_row):
        out = _run_tool({'conversation_id': 'c1'})
        assert _is_raw(out), (
            'a bare get_conversation call returned the prose transcript — the '
            'model is back to needing raw=true to see the record')

    def test_the_default_read_carries_the_metadata_prose_drops(self, fake_row):
        """The POINT of the default: the debugging fields are present."""
        out = _run_tool({'conversation_id': 'c1'})
        body = out.split('```json', 1)[1].rsplit('```', 1)[0]
        rec = json.loads(body)  # must parse — a cut dump is a dead end
        assert rec['msg_count'] == 2
        assert rec['rev'] == 3
        assert rec['settings']['preset'] == 'sonnet'
        assistant = rec['messages'][1]
        for field in ('finishReason', 'model', 'usage', '_msgId',
                      'modifiedFileList', 'toolRounds'):
            assert field in assistant, f'{field!r} missing from a default read'

    def test_explicit_true_is_the_same_read(self, fake_row):
        assert _run_tool({'conversation_id': 'c1', 'raw': True}) == \
            _run_tool({'conversation_id': 'c1'})


class TestProseIsStillReachable:
    def test_raw_false_returns_the_transcript(self, fake_row):
        out = _run_tool({'conversation_id': 'c1', 'raw': False})
        assert not _is_raw(out)
        assert 'Referenced Conversation' in out
        assert 'why did the cache break' in out

    @pytest.mark.parametrize('val', ['false', 'False', '0', 'no', 'off'])
    def test_stringified_false_is_honoured(self, fake_row, val):
        """A model may emit the flag as a JSON string; ``"false"`` is not True."""
        out = _run_tool({'conversation_id': 'c1', 'raw': val})
        assert not _is_raw(out), (
            f'raw={val!r} was read as truthy — the opt-out silently fails and '
            f'the caller gets the opposite of what it asked for')

    @pytest.mark.parametrize('val', ['true', 'True', '1', 'yes'])
    def test_stringified_true_still_means_raw(self, fake_row, val):
        assert _is_raw(_run_tool({'conversation_id': 'c1', 'raw': val}))


class TestLibraryDefaultUnchanged:
    """The tool flip must NOT reach the prose consumers.

    ``resolve_conv_refs`` (@-mention injection) and ``export_conv`` (the human
    export route) call ``get_conversation`` directly and want the readable
    transcript. If the library default moved with the tool default, an
    @-mentioned conversation would be injected into the prompt as a JSON dump.
    """

    def test_library_function_still_defaults_to_prose(self, fake_row):
        from lib.conv_ref import get_conversation
        out = get_conversation('c1')
        assert not _is_raw(out)
        assert 'Referenced Conversation' in out

    def test_mention_injection_path_gets_prose(self, fake_row):
        from lib.chat.messages import resolve_conv_refs
        got = resolve_conv_refs([{'id': 'c1', 'title': 'Cache bug'}])
        assert len(got) == 1
        assert not _is_raw(got[0]['text']), (
            'an @-mentioned conversation is now injected as a raw JSON dump — '
            'the tool-surface default leaked into the prompt-assembly path')


class TestCardAndPayloadAgree:
    """One resolver, so the rendered badge can never describe the wrong mode."""

    def _handler_digest(self, fn_args):
        """Capture the REAL _post_build closure's digest for these args.

        Stubs only ``simple_call`` (the executor seam) so the handler's own
        digest logic runs for real — mirrors test_conv_ref_raw.py.
        """
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
                {'convId': None}, {}, 'get_conversation', 't', fn_args,
                1, {}, {}, '/tmp/x', False,
            )
            meta = {}
            captured['post_build'](meta, 'PAYLOAD', fn_args)
            return meta.get('convDigest')
        finally:
            brain.simple_call = orig

    @pytest.mark.parametrize('fn_args', [
        {'conversation_id': 'c1'},                   # default → raw
        {'conversation_id': 'c1', 'raw': True},
        {'conversation_id': 'c1', 'raw': False},
        {'conversation_id': 'c1', 'raw': 'false'},   # stringified opt-out
    ])
    def test_badge_matches_the_payload_that_was_returned(self, fake_row, fn_args):
        digest = self._handler_digest(fn_args)
        assert digest is not None, 'the human card went missing'
        badged_raw = digest.get('raw') is True
        payload_raw = _is_raw(_run_tool(fn_args))
        assert badged_raw is payload_raw, (
            f'args={fn_args}: card badge raw={badged_raw} but the payload was '
            f'raw={payload_raw} — the card describes a mode that never ran')

    def test_default_read_is_badged_raw(self, fake_row):
        """Concrete floor for the parity test above (which a two-sided
        regression could satisfy by flipping BOTH to prose)."""
        assert self._handler_digest({'conversation_id': 'c1'}).get('raw') is True


class TestSchemaDescribesTheDefault:
    """The model only knows the default from the schema text.

    A correct implementation whose description still says "Default: false"
    teaches the model to pass raw=true redundantly — and, worse, to believe a
    bare call gives prose. Asserted as a CONTRACT (the schema must not claim
    raw is off by default) rather than by matching exact marketing wording.
    """

    def _schema(self):
        from lib.tools import CONV_REF_GET_TOOL
        return CONV_REF_GET_TOOL['function']

    def test_raw_param_does_not_advertise_a_false_default(self):
        desc = self._schema()['parameters']['properties']['raw']['description']
        low = desc.lower()
        assert 'default: false' not in low and 'default false' not in low, (
            f'the raw parameter still advertises a false default: {desc!r}')
        assert 'default' in low, (
            'the raw parameter says nothing about which mode you get when you '
            'omit it — the one fact a caller needs')

    def test_description_does_not_call_prose_the_default(self):
        desc = self._schema()['description'].lower()
        assert 'default (raw=false)' not in desc, (
            'the tool description still presents the prose transcript as the '
            'default mode')
