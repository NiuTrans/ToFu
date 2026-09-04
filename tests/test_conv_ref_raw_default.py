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
        'messages': MESSAGES, 'created_at': 1, 'updated_at': 2,
        'settings': {'preset': 'sonnet'},
        'msg_count': len(MESSAGES), 'rev': 3,
    })
    monkeypatch.setattr(
        _detail, '_read_conversation_snapshot',
        lambda conversation_id, *, user_id, **projection: row)
    return row


def _run_tool(fn_args):
    """Drive the REAL tool executor (not the library function directly)."""
    from lib.conv_ref import execute_conv_ref_tool
    return execute_conv_ref_tool('get_conversation', fn_args, user_id=1)


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
        out = get_conversation('c1', user_id=1)
        assert not _is_raw(out)
        assert 'Referenced Conversation' in out

    def test_mention_injection_path_gets_prose(self, fake_row):
        from lib.chat.messages import resolve_conv_refs
        got = resolve_conv_refs(
            [{'id': 'c1', 'title': 'Cache bug'}], user_id=1)
        assert len(got) == 1
        assert not _is_raw(got[0]['text']), (
            'an @-mentioned conversation is now injected as a raw JSON dump — '
            'the tool-surface default leaked into the prompt-assembly path')


class TestToolPayloadIsTheOnlyResultAuthority:
    """The handler must not synthesize a second-read display replacement."""

    def _handler_post_build(self, fn_args):
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
            return captured.get('post_build')
        finally:
            brain.simple_call = orig

    @pytest.mark.parametrize('fn_args', [
        {'conversation_id': 'c1'},                   # default → raw
        {'conversation_id': 'c1', 'raw': True},
        {'conversation_id': 'c1', 'raw': False},
        {'conversation_id': 'c1', 'raw': 'false'},   # stringified opt-out
    ])
    def test_handler_has_no_independent_digest(self, fake_row, fn_args):
        assert self._handler_post_build(fn_args) is None
        # The actual payload mode still follows the requested/default contract;
        # it is simply no longer replaced in the frontend by a later DB read.
        assert _is_raw(_run_tool(fn_args)) is (fn_args.get('raw') is not False
                                               and fn_args.get('raw') != 'false')


class TestSchemaDescribesTheDefault:
    """The model only knows the default from the schema text.

    A correct implementation whose description still says "Default: false"
    teaches the model to pass raw=true redundantly — and, worse, to believe a
    bare call gives prose. Asserted as a CONTRACT (the schema must not claim
    raw is off by default) rather than by matching exact marketing wording.
    """

    def _schema(self):
        from lib.tools.conversation import CONV_REF_GET_TOOL
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

    def test_schema_keeps_retrieval_safety_and_window_contracts(self):
        schema = self._schema()
        desc = schema['description'].lower()
        props = schema['parameters']['properties']
        raw = props['raw']['description'].lower()
        details = props['include_tool_details']['description'].lower()
        limit = props['limit']['description'].lower()
        before = props['before']['description'].lower()

        assert 'explicitly' in desc and 'never call proactively' in desc
        assert 'raw' in desc and 'defaults to true' in desc
        assert 'metadata' in desc and 'tool rounds' in desc
        assert 'raw=false' in desc and 'drops' in desc and 'condenses' in desc
        assert 'head+tail' in desc and 'whole messages' in desc
        assert 'delivered n of m' in desc and 'before=n' in desc
        assert 'default true' in raw and 'all fields' in raw
        assert 'raw=false' in details and 'default true' in details
        assert 'whole-message' in limit and 'clamp' in limit and 'header' in limit
        assert 'exclusive' in before and '1-based' in before
        assert 'omit for latest' in before and 'never pass 0' in before

    def test_schema_stays_within_always_paid_token_budget(self):
        from lib.tools.conversation import CONV_REF_GET_TOOL
        from lib.tools.gateway import tool_schema_tokens

        tokens = tool_schema_tokens([CONV_REF_GET_TOOL])
        assert tokens <= 350, (
            f'get_conversation schema costs {tokens} tokens; compact repeated '
            'raw/window prose without removing its safety or paging contracts')
