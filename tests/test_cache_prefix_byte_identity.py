"""tests/test_cache_prefix_byte_identity.py — client-side prefix-cache byte invariants.

Companion regression suite to the mrne3bqe "R4/R5 cache_read=0, R6 rebounds to
90%" investigation (see JOURNAL + debug/cache_retry_rebuild_byte_probe.py and
debug/cache_byte_probe_diff.py). The user's directive: prove the miss is
CLIENT-side and lock the fix at the byte level, NOT at the wire-fingerprint
level (canonical_messages deliberately erases the very transforms under
suspicion, so asserting on it would beg the question).

Two invariants are guarded here, both on the ACTUAL anthropic-translated wire
bytes (only ``cache_control`` stripped — the gateway does not tokenize the
marker; its POSITION is guarded separately by marker_signature):

  A. WIRE PREFIX STABILITY across an appending tool loop. Replaying the REAL
     mrne3bqe round structure (llmRound 0-2 signed thinking; llmRound 3 = a
     single tool_call with NO thinking / NO signature / NO assistant content —
     the live ``content=0 thinking=0`` round; llmRound 4 signed again) through
     the production reconstruction + build_body + add_cache_breakpoints +
     openai_body_to_anthropic pipeline must keep every already-sent prefix
     message BYTE-IDENTICAL round-over-round. (This documents the exoneration:
     the reachable pipeline is a byte fixpoint for this conv's shape.)

  B. NO CALLER-LIST MUTATION. ``build_body`` must not mutate the caller's
     persistent ``messages`` list. It USED to for image turns:
     ``_strip_non_api_fields`` copied only the top-level message dict but SHARED
     the nested ``content`` list by reference, and the in-place
     ``_downscale_oversized_images`` then re-encoded the image THROUGH that
     shared ref — so the orchestrator's persistent prefix message changed bytes
     the first time an oversized image was built, and the NEXT round sent a
     different prefix for an already-cached message → full re-bill. Same class
     for ``_inject_gemini_thought_signatures``, which writes into
     ``tool_calls[0]['extra_content']`` in place. FIXED: ``_strip_non_api_fields``
     now deep-copies nested mutable values, isolating the cleaned messages from
     the caller's list. Guarded here as hard PASS tests (image + tool_calls),
     with the deep-copy mechanism pinned in
     ``test_strip_non_api_fields_deepcopies_nested_content``.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_cache_prefix_byte_identity.py -p no:cacheprovider
"""

import copy
import json

import pytest

pytestmark = pytest.mark.unit


MODEL = 'aws.claude-opus-4.8'


# ── raw-byte serialization of the translated wire (strip ONLY cache_control:
#    the server ignores the marker key; its byte POSITION is a separate axis) ──
def _strip_cc(o):
    if isinstance(o, dict):
        return {k: _strip_cc(v) for k, v in o.items() if k != 'cache_control'}
    if isinstance(o, list):
        return [_strip_cc(x) for x in o]
    return o


def _msg_wire_bytes(body):
    out = []
    for m in body.get('messages') or []:
        out.append(json.dumps(_strip_cc(m), ensure_ascii=False,
                              sort_keys=False).encode('utf-8'))
    return out


def _tools():
    return [{'type': 'function',
             'function': {'name': n, 'description': f'{n} tool',
                          'parameters': {'type': 'object',
                                         'properties': {'x': {'type': 'string'}}}}}
            for n in ('grep_search', 'read_files', 'fetch_url')]


# The REAL mrne3bqe toolRounds shape (queried from the stored conversation):
# llmRound 3 (=R4) is the pivotal single tool_call with no thinking / no sig.
_REAL_ROUNDS = [
    {'toolCallId': 'c0a', 'toolName': 'grep_search', 'status': 'done',
     'toolContent': 'r0a ' + ('p ' * 40), 'toolArgs': json.dumps({'pattern': 'a'}),
     'assistantContent': 'Looking.', 'thinking': 'think0 ' + ('d ' * 15),
     'thinkingSignature': 'sig0', 'llmRound': 0},
    {'toolCallId': 'c0b', 'toolName': 'grep_search', 'status': 'done',
     'toolContent': 'r0b ' + ('p ' * 40), 'toolArgs': json.dumps({'pattern': 'b'}),
     'llmRound': 0},
    {'toolCallId': 'c1a', 'toolName': 'grep_search', 'status': 'done',
     'toolContent': 'r1a ' + ('p ' * 40), 'toolArgs': json.dumps({'pattern': 'c'}),
     'assistantContent': 'Next.', 'thinking': 'think1 ' + ('d ' * 15),
     'thinkingSignature': 'sig1', 'llmRound': 1},
    {'toolCallId': 'c2a', 'toolName': 'read_files', 'status': 'done',
     'toolContent': 'r2a ' + ('p ' * 40), 'toolArgs': json.dumps({'path': 'x'}),
     'assistantContent': 'Reading.', 'thinking': 'think2 ' + ('d ' * 15),
     'thinkingSignature': 'sig2', 'llmRound': 2},
    # ★ llmRound 3 = R4: single tool call, NO thinking, NO sig, NO content
    {'toolCallId': 'c3a', 'toolName': 'read_files', 'status': 'done',
     'toolContent': 'r3a ' + ('p ' * 40), 'toolArgs': json.dumps({'path': 'y'}),
     'llmRound': 3},
    {'toolCallId': 'c4a', 'toolName': 'fetch_url', 'status': 'done',
     'toolContent': 'r4a ' + ('p ' * 40), 'toolArgs': json.dumps({'url': 'z'}),
     'assistantContent': 'Fetch.', 'thinking': 'think4 ' + ('d ' * 15),
     'thinkingSignature': 'sig4', 'llmRound': 4},
]

_HEAD = [{'role': 'system', 'content': 'You are an agent. ' + ('g ' * 200)},
         {'role': 'user', 'content': 'Investigate. ' + ('c ' * 40)}]


def _wire_for_rounds(upto_llm, tools):
    """Reconstruct + build + breakpoint + translate for the conversation as it
    stands after llmRound ``upto_llm`` completed (the Continue-path shape)."""
    from lib.tasks_pkg.conv_message_builder._toolcalls import (
        _reconstruct_tool_call_messages)
    from lib.llm import build_body, add_cache_breakpoints
    from lib.llm.anthropic_outbound import openai_body_to_anthropic

    batch = [r for r in _REAL_ROUNDS if r['llmRound'] <= upto_llm]
    recon = _reconstruct_tool_call_messages(batch) or []
    mlist = copy.deepcopy(_HEAD) + copy.deepcopy(recon)
    body = build_body(MODEL, mlist, max_tokens=2048, thinking_enabled=True,
                      thinking_depth='high', tools=tools, stream=True)
    body['_task_id'] = ''
    add_cache_breakpoints(body, log_prefix='[test]')
    return openai_body_to_anthropic(body)


# ═══════════════════════════════════════════════════════════════════════════
#  Invariant A — wire prefix byte-stability across the appending tool loop
# ═══════════════════════════════════════════════════════════════════════════

def test_wire_prefix_byte_identical_across_appending_rounds():
    """Every already-sent prefix message stays BYTE-IDENTICAL as later rounds
    are appended — including across the no-thinking R4 (llmRound 3) round.

    This is the direct wire-byte assertion the wire fingerprint cannot make
    (it erases str↔block, arg order, markers). If ANY prefix message flips
    bytes when the conversation grows, that is a client-caused cache miss.
    """
    tools = _tools()
    prev = None
    for upto in range(0, 5):
        cur = _msg_wire_bytes(_wire_for_rounds(upto, tools))
        if prev is not None:
            shared = len(prev)  # every message of the previous round is prefix now
            for i in range(min(shared, len(cur))):
                assert prev[i] == cur[i], (
                    f'prefix message[{i}] changed bytes when appending '
                    f'llmRound {upto} — client-caused prefix mutation.\n'
                    f'prev={prev[i][:200]!r}\ncur ={cur[i][:200]!r}')
        prev = cur


def test_no_thinking_round_replays_stably():
    """The R4 (llmRound 3) no-thinking / no-signature single tool_call round
    replays with a stable ``tool_use`` block whether it is the newest round or
    a buried prefix round — a thinking block is NEVER fabricated for it (which
    would flip its bytes once it stops being the tail)."""
    tools = _tools()
    body_r4 = _wire_for_rounds(3, tools)   # R4 is the newest round
    body_r5 = _wire_for_rounds(4, tools)   # R4 is now a prefix round

    def _r4_assistant(body):
        for m in body.get('messages') or []:
            if m.get('role') != 'assistant':
                continue
            blocks = m.get('content')
            if isinstance(blocks, list):
                for b in blocks:
                    if (isinstance(b, dict) and b.get('type') == 'tool_use'
                            and b.get('name') == 'read_files'
                            and b.get('id') == 'c3a'):
                        return m
        return None

    a4 = _r4_assistant(body_r4)
    a5 = _r4_assistant(body_r5)
    assert a4 is not None and a5 is not None, 'R4 assistant tool_use not found'
    # No thinking block fabricated for the unsigned round, in either position.
    for m in (a4, a5):
        types = [b.get('type') for b in m['content'] if isinstance(b, dict)]
        assert 'thinking' not in types, (
            f'a thinking block was fabricated for the unsigned R4 round: {types}')
    # Byte-identical (ignoring the tail cache_control marker).
    assert (json.dumps(_strip_cc(a4), ensure_ascii=False, sort_keys=False)
            == json.dumps(_strip_cc(a5), ensure_ascii=False, sort_keys=False)), (
        'R4 assistant turn flips bytes between tail and prefix position')


# ═══════════════════════════════════════════════════════════════════════════
#  Invariant B — build_body must NOT mutate the caller's persistent list
#  (the shared-nested-ref image write-through prefix-mutation defect)
# ═══════════════════════════════════════════════════════════════════════════

def _oversized_png_uri(px=2200):
    try:
        import base64 as _b64
        import io
        from PIL import Image
    except ImportError:
        return None
    img = Image.new('RGB', (px, int(px * 0.6)), (30, 160, 90))
    for x in range(0, px, 40):
        for y in range(0, int(px * 0.6), 40):
            img.putpixel((x, y), ((x * 7) % 256, (y * 5) % 256, (x + y) % 256))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + _b64.b64encode(buf.getvalue()).decode('ascii')


def _image_url_in(msg):
    for b in msg.get('content') or []:
        if isinstance(b, dict) and b.get('type') == 'image_url':
            return b['image_url']['url']
    return None


def test_strip_non_api_fields_deepcopies_nested_content():
    """Pin the FIX: _strip_non_api_fields deep-copies nested mutable values
    (``content`` block lists, ``tool_calls``) instead of sharing them by
    reference. This is the seam that closes the write-through — an in-place
    downscale / signature-injection on the cleaned message can no longer reach
    the caller's persistent list.
    """
    from lib.llm_sanitize import _strip_non_api_fields
    src = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
    cleaned = _strip_non_api_fields(src)
    # The nested content list must be an independent copy, not the caller's.
    assert cleaned[0]['content'] is not src[0]['content'], (
        'content list is still shared by reference — the deep-copy fix in '
        '_strip_non_api_fields regressed; in-place build_body mutations will '
        'leak back into the caller (see test_build_body_does_not_mutate_caller_images).')
    # …but its VALUE is equal (a faithful copy, nothing dropped).
    assert cleaned[0]['content'] == src[0]['content']
    # Mutating the copy must NOT touch the source.
    cleaned[0]['content'][0]['text'] = 'MUTATED'
    assert src[0]['content'][0]['text'] == 'hi', (
        'mutating the cleaned copy wrote through to the caller\'s list')


def test_build_body_does_not_mutate_caller_images():
    """build_body must treat the caller's ``messages`` as read-only. Today it
    re-encodes an oversized image in place, changing the persistent prefix
    message's bytes → next round re-bills the whole body uncached."""
    uri = _oversized_png_uri()
    if uri is None:
        pytest.skip('Pillow not installed — image downscale path unavailable')
    from lib.llm import build_body

    persistent = [
        {'role': 'system', 'content': 'Sys ' + ('g ' * 40)},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': 'look'},
            {'type': 'image_url', 'image_url': {'url': uri}}]},
    ]
    before = _image_url_in(persistent[1])
    build_body(MODEL, persistent, max_tokens=2048, thinking_enabled=True,
               thinking_depth='high', tools=_tools(), stream=True)
    after = _image_url_in(persistent[1])
    assert before == after, (
        'build_body mutated the caller\'s persistent image bytes '
        f'({len(before)} → {len(after)} chars) — this shifts the prompt-cache '
        'prefix on the next round for an already-cached message.')


def test_build_body_does_not_mutate_caller_tool_calls():
    """The other write-through mutator: on a Gemini fallback,
    ``_inject_gemini_thought_signatures`` writes ``extra_content.google.
    thought_signature`` into ``tool_calls[0]`` in place. With the deep-copy fix
    that write lands on the cleaned copy only — the caller's persistent
    assistant turn (produced by a non-Gemini model) keeps its original
    tool_calls, so its prefix bytes don't shift on the next round."""
    from lib.llm import build_body
    from lib.model_info import is_gemini

    gemini_model = 'gemini-3-pro'
    if not is_gemini(gemini_model):
        pytest.skip('gemini-3-pro not classified as Gemini in this build')

    persistent = [
        {'role': 'system', 'content': 'Sys ' + ('g ' * 40)},
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': 'calling',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'grep_search', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'res'},
    ]
    tc_before = copy.deepcopy(persistent[2]['tool_calls'])
    build_body(gemini_model, persistent, max_tokens=2048, stream=True,
               tools=_tools())
    assert persistent[2]['tool_calls'] == tc_before, (
        'build_body injected a thought_signature into the caller\'s persistent '
        'tool_calls — this shifts the prompt-cache prefix for an already-cached '
        'assistant turn on the next round.')
    # No extra_content leaked into the caller's tool_call.
    assert 'extra_content' not in persistent[2]['tool_calls'][0], (
        'extra_content.google.thought_signature wrote through to the caller')


def test_image_downscale_is_a_byte_fixpoint_once_capped():
    """Positive control for the downscaler itself: a second pass on an already-
    downscaled (≤cap) image is a no-op, and two independent first-encodes are
    deterministic. (So the ONE-TIME write-through is the whole defect — there is
    no per-round re-encode churn once capped.)"""
    uri = _oversized_png_uri()
    if uri is None:
        pytest.skip('Pillow not installed')
    from lib.llm import _downscale_oversized_images

    def mk():
        return [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'x'},
            {'type': 'image_url', 'image_url': {'url': uri}}]}]

    m = mk()
    _downscale_oversized_images(m, MODEL)
    once = _image_url_in(m[0])
    _downscale_oversized_images(m, MODEL)   # second pass — must be a no-op
    twice = _image_url_in(m[0])
    assert once == twice, 'downscale is not idempotent on an already-capped image'

    ma, mb = mk(), mk()
    _downscale_oversized_images(ma, MODEL)
    _downscale_oversized_images(mb, MODEL)
    assert _image_url_in(ma[0]) == _image_url_in(mb[0]), (
        'downscale re-encode is non-deterministic across independent runs')


# ═══════════════════════════════════════════════════════════════════════════
#  Invariant C — cache_control TTL must NOT flip across an in-task RETRY
#  (THE reproducible mrne3bqe false-negative: _task_id popped after attempt 1
#   → add_cache_breakpoints falls back to the LIVE global CACHE_EXTENDED_TTL,
#   which can disagree with the task's latched decision → ttl 1h↔bare flip →
#   different Anthropic cache key → full miss. canonical_messages STRIPS
#   cache_control and markers_regressed only checks COUNT, so the detector
#   mislabels it "server-side PROVEN".)
# ═══════════════════════════════════════════════════════════════════════════

def _system_ttl(body):
    """The cache_control marker on the hoisted system prefix (Anthropic body)."""
    s = body.get('system')
    if isinstance(s, list):
        for b in s:
            if isinstance(b, dict) and b.get('cache_control'):
                return b['cache_control']
    return None


def _build_translated_with_task(mlist, tools, task_id):
    """Mirror prepare_request's per-attempt sequence: read _task_id for the
    latch, add_cache_breakpoints (which POPS _task_id), then translate."""
    from lib.llm import build_body, add_cache_breakpoints
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    body = build_body(MODEL, copy.deepcopy(mlist), max_tokens=2048,
                      thinking_enabled=True, thinking_depth='high',
                      tools=copy.deepcopy(tools), stream=True)
    if task_id is not None:
        body['_task_id'] = task_id
    add_cache_breakpoints(body, log_prefix='[test-ttl]')
    return openai_body_to_anthropic(body)


@pytest.fixture
def _ttl_env():
    """Isolate the global CACHE_EXTENDED_TTL flag + the per-task latch table so
    the flip test can drive them without leaking into other tests."""
    import lib as _lib
    from lib.tasks_pkg.cache_tracking import _ttl as _ttlmod
    _orig_flag = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
    _orig_latch = dict(_ttlmod._ttl_latch)
    yield _lib, _ttlmod
    _lib.CACHE_EXTENDED_TTL = _orig_flag
    _ttlmod._ttl_latch.clear()
    _ttlmod._ttl_latch.update(_orig_latch)


def test_cache_ttl_stable_across_in_task_retry(_ttl_env):
    """A retried attempt within the SAME task must stamp the SAME cache_control
    TTL as the first attempt — the task's LATCHED decision — regardless of the
    live global flag.

    ROOT-CAUSE REGRESSION (mrne3bqe): the streaming retry loop
    (lib/llm/stream.py:62) re-feeds the SAME body dict to prepare_request →
    add_cache_breakpoints on every 429/503 attempt. The fix reads
    ``body['_task_id']`` NON-destructively (no pop) and strips it only at the
    OpenAI serialization boundary, so every attempt sees the same latch key and
    the TTL/beta marker can no longer flip mid-task. We drive the REAL live path
    here: build the body ONCE (as the orchestrator does), then run
    prepare_request TWICE against that same object (attempt 0, then a retry
    attempt) with the global flag drifted between them; the tail cache_control
    TTL must be identical across both attempts.
    """
    import lib as _liba
    from lib.llm import build_body
    from lib.llm._sse_core import prepare_request

    _lib, _ttlmod = _ttl_env
    tools = _tools()
    mlist = copy.deepcopy(_HEAD) + [
        {'role': 'assistant', 'content': 'a', 'reasoning_content': 't',
         'thinking_signature': 's',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'grep_search', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'res ' + ('x ' * 30)},
    ]
    task_id = 'task_ttl_latch'

    # Task starts while the global is ON → latch True for the task's life.
    _liba.CACHE_EXTENDED_TTL = True
    _ttlmod._ttl_latch.clear()
    from lib.tasks_pkg.cache_tracking import latch_extended_ttl
    latch_extended_ttl(task_id)

    # The orchestrator builds the body ONCE per round with _task_id set.
    body = build_body(MODEL, copy.deepcopy(mlist), max_tokens=2048,
                      thinking_enabled=True, thinking_depth='high',
                      tools=copy.deepcopy(tools), stream=True)
    body['_task_id'] = task_id

    # Attempt 0 (anthropic path): prepare_request annotates + translates.
    plan1 = prepare_request(body, attempt=0, log_prefix='[t0]',
                            base_url='https://x', api_protocol='anthropic')
    ttl1 = _system_ttl(plan1.body)

    # The global drifts OFF mid-task (settings toggle / default drift).
    _liba.CACHE_EXTENDED_TTL = False

    # Attempt 1 = the in-task RETRY: SAME body object re-fed to prepare_request.
    # With the fix, _task_id still rides `body`, so the latch (True) is honored
    # again and the TTL does NOT flip.
    plan2 = prepare_request(body, attempt=1, log_prefix='[t1]',
                            base_url='https://x', api_protocol='anthropic')
    ttl2 = _system_ttl(plan2.body)

    assert ttl1 == ttl2, (
        f'cache_control TTL flipped across an in-task retry: {ttl1} → {ttl2}. '
        'The retried attempt lost the task latch and read the live global flag '
        '→ different Anthropic cache key → full prefix miss that '
        'canonical_messages (strips cache_control) mislabels "server-side".')
    # And it must be the LATCHED (1h) decision, not the drifted global.
    assert ttl1 == {'type': 'ephemeral', 'ttl': '1h'}, (
        f'expected the latched 1h TTL on both attempts, got {ttl1}')


def test_ttl_flip_neuter_is_detected(_ttl_env):
    """NEUTER / negative control: PROVE the byte-level guard actually bites.

    We deliberately induce the flip (latch True, global drifts False, retry
    loses _task_id) and assert the two translated bodies REALLY differ in bytes
    when cache_control is KEPT — and that stripping cache_control (what
    canonical_messages does) HIDES it. This demonstrates both that the defect is
    real AND why the wire fingerprint reports a false 'byte-identical'.
    """
    _lib, _ttlmod = _ttl_env
    tools = _tools()
    mlist = copy.deepcopy(_HEAD) + [
        {'role': 'assistant', 'content': 'a', 'reasoning_content': 't',
         'thinking_signature': 's',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'grep_search', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'res ' + ('x ' * 30)},
    ]
    task_id = 'task_ttl_neuter'
    _lib.CACHE_EXTENDED_TTL = True
    _ttlmod._ttl_latch.clear()
    from lib.tasks_pkg.cache_tracking import latch_extended_ttl
    latch_extended_ttl(task_id)
    w1 = _build_translated_with_task(mlist, tools, task_id)
    _lib.CACHE_EXTENDED_TTL = False
    w2 = _build_translated_with_task(mlist, tools, task_id=None)

    def _dump(body, *, keep_cc):
        obj = body if keep_cc else _strip_cc(body)
        return json.dumps({'system': obj.get('system'), 'tools': obj.get('tools'),
                           'messages': obj.get('messages')},
                          ensure_ascii=False, sort_keys=False).encode('utf-8')

    # With cache_control KEPT the bytes MUST differ (the flip is real).
    assert _dump(w1, keep_cc=True) != _dump(w2, keep_cc=True), (
        'expected a real byte difference from the TTL flip but found none — '
        'the guard would not bite')
    # With cache_control STRIPPED (what canonical_messages does) it is hidden —
    # this is precisely the false-negative that mislabels the miss server-side.
    assert _dump(w1, keep_cc=False) == _dump(w2, keep_cc=False), (
        'stripping cache_control should HIDE the flip (reproducing the wire-'
        'fingerprint blind spot); if it no longer hides it, the fingerprint '
        'was changed to include ttl — update this control.')
