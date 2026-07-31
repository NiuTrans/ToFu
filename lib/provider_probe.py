"""Access-matrix cell-probe engine — server-owned background reachability test.

Moved out of ``routes/config.py`` (2026-06). Sends a 1-token completion to
every (key × concrete-model-id) cell of a provider to learn which pairs the
gateway actually routes. Because each alias on a gateway can route to a
genuinely DIFFERENT upstream model, every alias is probed independently.

The probe is a long-running fan-out: it runs in a background thread and its
progress is persisted to disk (``data/config/probe_cache/``) as a secret-free
snapshot, so closing Settings or restarting the server doesn't lose progress.

``routes/config.py`` re-exports every public name here under its legacy
private alias (``_probe_one_cell``, ``_probe_cell_multi``,
``_run_cell_probe_task``, ``_probe_cache_path``, ``_probe_cell_key``,
``_persist_probe_task``, ``_public_probe_snapshot``, ``CELL_PROBE_TASKS``,
``CELL_PROBE_LOCK``, ``_time``) so existing call sites and tests keep working.

NOTE for tests: ``probe_cell_multi`` / ``run_cell_probe_task`` call
``probe_one_cell`` THROUGH THIS MODULE'S global, so
``mock.patch.object(lib.provider_probe, 'probe_one_cell', ...)`` (or patching
the re-exported name) takes effect. Same for ``probe_cache_path``.
"""

import hashlib
import io
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.config_dir import config_path as _config_path
from lib.json_store import read_json, write_json_atomic  # noqa: F401  (read_json re-used by routes)
from lib.log import get_logger
from lib.model_info.capability_taxonomy import is_chat_model as _is_chat_model

# Verdict for cells whose capabilities have no probe surface we implement
# (anything outside image_gen / embedding / transcription). Chat-probing a
# non-chat model is a guaranteed false positive (the Meituan gateway's Java
# router hits ``Random.nextInt(0)`` → "bound must be positive" when the
# chat-binding candidate list is empty), so unknown non-chat cells are
# skipped rather than chat-probed. Known non-chat modalities are probed via
# their REAL endpoint — see nonchat_probe_fn().
SKIPPED = 'skipped'

# Image cells generate one real (tiny) billed image per probe, so they get a
# single attempt regardless of the matrix attempts selector — multiplying a
# billed generation to filter a rare transient 429 is a bad trade.
_IMAGE_PROBE_MIN_TIMEOUT = 60  # generation routinely takes 10-40s

logger = get_logger(__name__)


def probe_one_cell(base_url, api_key, model_id, extra_headers, timeout,
                   protocol='openai', oauth=''):
    """Send a minimal completion to test one (key, model) pair.

    Returns one of: 'ok', 'rate_limited', 'unauthorized', 'not_found',
    'unavailable', 'error' plus a short human-readable detail string.

    A 200 OR an HTTP 400 both count as ``ok`` — a 400 means the gateway
    accepted the (key, model) routing and only rejected the (deliberately
    tiny) request shape, which still proves the pair is reachable.

    ``protocol='anthropic'`` probes the Anthropic Messages API
    (``POST /v1/messages`` with ``x-api-key`` + ``anthropic-version``)
    instead of OpenAI Chat Completions. The status→verdict table is
    identical for both protocols.

    ``oauth`` (``'claude'``/``'codex'``) marks a SUBSCRIPTION provider whose
    configured api_key is the 'oauth-managed' SENTINEL — probing with it
    literally would return a 401 and wrongly flag a working subscription as
    recommend-disable. We resolve the live per-request token via
    :func:`lib.oauth.outbound.resolve_oauth_request` and send it as
    ``x-api-key`` (Anthropic's 2026 block rejects ``Authorization: Bearer``
    for subscription tokens). A provider with no usable token is reported as
    the NEUTRAL verdict ``'not_logged_in'`` — never a model fault, never
    recommend-disable.
    """
    from lib.http_client import http_post

    # ── Subscription (OAuth) providers ──────────────────────────────────
    if oauth:
        if oauth == 'codex':
            # Codex is Responses-API STREAMING only; the app's translator has
            # no non-stream path, so a non-stream probe would be a guaranteed
            # false negative. Skipping is the honest verdict.
            return SKIPPED, 'codex subscription is stream-only — no non-stream probe'
        from lib.llm.anthropic_outbound import anthropic_messages_url
        from lib.oauth.outbound import claude_oauth_url, resolve_oauth_request
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': '.'}],
            'max_tokens': 1,
        }
        try:
            token, hdrs, body = resolve_oauth_request(oauth, payload,
                                                      extra_headers)
        except RuntimeError as e:
            # Not logged in / refresh failed — a SESSION state, not a model
            # fault. Distinct from an authenticated 401.
            logger.info('[CellProbe] %s @ %s oauth token unavailable: %s',
                        model_id, base_url, e)
            return 'not_logged_in', str(e)[:160]
        url = claude_oauth_url(anthropic_messages_url(base_url))
        headers = {
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01',
        }
        headers.update(hdrs or {})          # beta flags, x-app, User-Agent
        headers.pop('Authorization', None)  # subscription tokens ride x-api-key
        headers['x-api-key'] = token
        payload = body                      # resolve prepends the identity block
        try:
            resp = http_post(url, json=payload, headers=headers, timeout=timeout)
        except Exception as e:
            logger.warning('[CellProbe] %s @ %s network error: %s',
                           model_id, base_url, e)
            return 'unavailable', 'network: %s' % str(e)[:120]
        code = resp.status_code
        try:
            resp_body = resp.text[:400]
        except (UnicodeDecodeError, ValueError, OSError) as e:
            logger.debug('[CellProbe] %s @ %s: could not read response body: %s',
                         model_id, base_url, e)
            resp_body = ''
        return _classify_status(code, resp_body)

    # ``max_tokens: 1`` is the floor — the probe only needs to learn whether
    # the gateway accepts the (key, model) routing, never the completion
    # itself, so output cost is held to a single token per attempt.
    if protocol == 'anthropic':
        from lib.llm.anthropic_outbound import (
            anthropic_headers, anthropic_messages_url,
        )
        url = anthropic_messages_url(base_url)
        headers = anthropic_headers(api_key, extra_headers)
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': '.'}],
            'max_tokens': 1,
        }
    else:
        url = base_url.rstrip('/') + '/chat/completions'
        headers = {'Authorization': 'Bearer %s' % api_key} if api_key else {}
        if extra_headers:
            headers.update(extra_headers)
        payload = {
            'model': model_id,
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 1,
            'stream': False,
        }
    try:
        resp = http_post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as e:
        logger.warning('[CellProbe] %s @ %s network error: %s', model_id, base_url, e)
        return 'unavailable', 'network: %s' % str(e)[:120]

    code = resp.status_code
    try:
        body = resp.text[:400]
    except (UnicodeDecodeError, ValueError, OSError) as e:
        logger.debug('[CellProbe] %s @ %s: could not read response body: %s',
                     model_id, base_url, e)
        body = ''
    return _classify_status(code, body)


def _classify_status(code: int, body: str):
    """Map an HTTP status (+body excerpt) to a (verdict, detail) pair.

    A 200 OR a 400 both count as ``ok`` — a 400 means the gateway accepted
    the (key, model) routing and only rejected the (deliberately tiny)
    request shape, which still proves the pair is reachable.
    """
    lower = body.lower()
    if code == 200 or code == 400:
        return 'ok', 'HTTP %d' % code
    if code == 429 or code == 402:
        return 'rate_limited', 'HTTP %d %.120s' % (code, body)
    if code in (401, 403):
        return 'unauthorized', 'HTTP %d %.120s' % (code, body)
    if code == 404 or 'model_not_found' in lower or 'does not exist' in lower or 'no such model' in lower:
        return 'not_found', 'HTTP %d %.120s' % (code, body)
    if code in (500, 502, 503, 504, 529):
        return 'unavailable', 'HTTP %d %.120s' % (code, body)
    return 'error', 'HTTP %d %.120s' % (code, body)


def _post_and_classify(url, headers, timeout, *, json_body=None, files=None,
                       data=None, validate=None, surface=''):
    """POST one modality probe and classify the answer.

    Shared tail for every non-chat probe: same transport error handling and
    status→verdict ladder as :func:`probe_one_cell`, plus an optional
    ``validate(parsed_json, raw_body)`` hook that inspects a 200 response's
    SHAPE (None = reachable). A 200 whose shape fails validation is an
    'error' (the endpoint answered but not in the dialect the app speaks).
    ``surface`` names the endpoint in the ok detail (e.g. '/embeddings').
    """
    from lib.http_client import http_post

    try:
        resp = http_post(url, json=json_body, files=files, data=data,
                         headers=headers, timeout=timeout)
    except Exception as e:
        logger.warning('[CellProbe] %s network error: %s', surface or url, e)
        return 'unavailable', 'network: %s' % str(e)[:120]
    code = resp.status_code
    try:
        body = resp.text[:400]
    except (UnicodeDecodeError, ValueError, OSError) as e:
        logger.debug('[CellProbe] %s: could not read response body: %s',
                     surface or url, e)
        body = ''
    if code == 200 and validate is not None:
        try:
            parsed = resp.json()
        except Exception as e:
            logger.debug('[CellProbe] %s: 200 but non-JSON body: %s', surface, e)
            parsed = None
        reason = validate(parsed, body)
        if reason:
            return 'error', 'HTTP 200 via %s — invalid shape (%s) %.120s' % (
                surface, reason, body)
    status, detail = _classify_status(code, body)
    if status == 'ok' and surface:
        detail = '%s via %s' % (detail, surface)
    return status, detail


# ── Non-chat modality probes ──────────────────────────────────────────

_SILENCE_WAV: bytes | None = None


def _silence_wav_bytes(duration_s: float = 0.3, rate: int = 16000) -> bytes:
    """A minimal valid PCM WAV of digital silence (16-bit mono).

    A transcription endpoint must accept it and answer 200 with an empty (or
    whitespace) transcript — enough to prove the (key, model) routing works
    without sending real speech. ~10 KB at the defaults.
    """
    global _SILENCE_WAV
    if _SILENCE_WAV is None:
        pcm = b'\x00\x00' * int(duration_s * rate)
        hdr = (b'RIFF' + (36 + len(pcm)).to_bytes(4, 'little') + b'WAVE'
               + b'fmt ' + (16).to_bytes(4, 'little')
               + (1).to_bytes(2, 'little')          # PCM
               + (1).to_bytes(2, 'little')          # mono
               + rate.to_bytes(4, 'little')
               + (rate * 2).to_bytes(4, 'little')   # byte rate
               + (2).to_bytes(2, 'little')          # block align
               + (16).to_bytes(2, 'little')         # bits/sample
               + b'data' + len(pcm).to_bytes(4, 'little'))
        _SILENCE_WAV = hdr + pcm
    return _SILENCE_WAV


def _auth_headers(api_key, extra_headers):
    headers = {'Authorization': 'Bearer %s' % api_key} if api_key else {}
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _validate_embedding(parsed, _raw):
    try:
        emb = (parsed or {}).get('data', [{}])[0].get('embedding')
    except (AttributeError, IndexError, TypeError) as _e:
        logger.debug('validate embedding: missing attribute/short/malformed/unexpected type (%s)', _e)
        return 'no data[0].embedding'
    return None if emb else 'empty embedding vector'


def _validate_transcription(parsed, _raw):
    return None if isinstance(parsed, dict) else 'non-JSON response'


def _validate_images_api(parsed, _raw):
    try:
        item = (parsed or {}).get('data', [{}])[0]
    except (AttributeError, IndexError, TypeError) as _e:
        logger.debug('validate images api: missing attribute/short/malformed/unexpected type (%s)', _e)
        return 'no data[0]'
    if item.get('b64_json') or item.get('url'):
        return None
    return 'no image payload (b64_json/url)'


def _validate_image_chat(parsed, _raw):
    try:
        content = (parsed or {})['choices'][0]['message'].get('content')
    except (AttributeError, IndexError, KeyError, TypeError) as _e:
        logger.debug('validate image chat: missing attribute/short/malformed/missing key/unexpected type (%s)', _e)
        return 'no choices[0].message.content'
    if isinstance(content, str):
        return None if content.strip() else 'empty text content'
    if isinstance(content, list):
        return None if content else 'empty content parts'
    return 'unexpected content type %s' % type(content).__name__


def probe_embedding_cell(base_url, api_key, model_id, extra_headers, timeout,
                         protocol='openai'):
    """Probe an embedding model via ``POST /embeddings`` (one short input).

    Same (base_url, api_key, model_id, extra_headers, timeout, protocol)
    signature as :func:`probe_one_cell` so ``probe_cell_multi`` can drive it;
    ``protocol`` is accepted and ignored (embeddings are OpenAI-shaped here).
    """
    url = base_url.rstrip('/') + '/embeddings'
    return _post_and_classify(
        url, _auth_headers(api_key, extra_headers), timeout,
        json_body={'model': model_id, 'input': 'ping'},
        validate=_validate_embedding, surface='/embeddings')


def probe_transcription_cell(base_url, api_key, model_id, extra_headers,
                             timeout, protocol='openai'):
    """Probe an ASR model via multipart ``POST /audio/transcriptions``
    carrying a 0.3s silence WAV (no real speech leaves the box)."""
    url = base_url.rstrip('/') + '/audio/transcriptions'
    files = {'file': ('probe.wav', io.BytesIO(_silence_wav_bytes()), 'audio/wav')}
    data = {'model': model_id, 'response_format': 'json'}
    return _post_and_classify(
        url, _auth_headers(api_key, extra_headers), timeout,
        files=files, data=data,
        validate=_validate_transcription, surface='/audio/transcriptions')


def probe_image_cell(base_url, api_key, model_id, extra_headers, timeout,
                     protocol='openai'):
    """Probe an image_gen model by generating one tiny image — the only
    definitive test, and it bills ~1 generation.

    Surface mirrors the app's own image path (:func:`lib.image_gen._slots`):
    ``openai_image`` slots POST ``/images/generations``; everything else
    POSTs ``/chat/completions`` with ``modalities: ['TEXT','IMAGE']``
    (gemini-style). Image models rejected by a plain chat probe (the
    'bound must be positive' incident) answer here because the gateway
    routes modalities-carrying requests to the image binding.
    """
    headers = _auth_headers(api_key, extra_headers)
    if protocol == 'openai_image':
        url = base_url.rstrip('/') + '/images/generations'
        payload = {'model': model_id,
                   'prompt': 'a single small red dot on a white background',
                   'n': 1}
        return _post_and_classify(url, headers, timeout, json_body=payload,
                                  validate=_validate_images_api,
                                  surface='/images/generations')
    url = base_url.rstrip('/') + '/chat/completions'
    payload = {
        'model': model_id,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'Draw a single small red dot.'}]}],
        'modalities': ['TEXT', 'IMAGE'],
        'stream': False,
    }
    return _post_and_classify(url, headers, timeout, json_body=payload,
                              validate=_validate_image_chat,
                              surface='image chat')


def _validate_tts(_parsed, raw):
    # /audio/speech answers with raw audio bytes (no JSON envelope). The
    # _post_and_classify harness hands us resp.text (decoded, best-effort)
    # — the container magic survives as ASCII/latin-1 chars.
    if not raw:
        return 'empty body'
    if raw[:4] == 'RIFF' or raw[:3] == 'ID3' or raw[:4] in ('fLaC', 'OggS'):
        return None
    if raw[0] == '\xff':  # MP3 frame sync (decoded as latin-1)
        return None
    return 'non-audio payload'


def probe_tts_cell(base_url, api_key, model_id, extra_headers, timeout,
                   protocol='openai'):
    """Probe a TTS model via ``POST /audio/speech`` (one word of input).

    Voice resolution mirrors the app's own path (lib.tts.default_voice):
    a configured deployment voice wins, otherwise the documented fallback
    is used — a provider that rejects the voice answers 400, which the
    classifier still counts as reachable (routing proven).
    """
    from lib.tts import default_voice
    url = base_url.rstrip('/') + '/audio/speech'
    payload = {'model': model_id, 'input': 'ping',
               'voice': default_voice(), 'response_format': 'wav'}
    return _post_and_classify(
        url, _auth_headers(api_key, extra_headers), timeout,
        json_body=payload, validate=_validate_tts, surface='/audio/speech')


def nonchat_probe_fn(caps):
    """Return the modality probe function for a non-chat caps list.

    Priority when a model carries several non-chat caps: image_gen >
    transcription > embedding. Returns None for capabilities with no
    implemented surface (caller keeps the 'skipped' verdict).
    """
    if not caps:
        return None
    if 'image_gen' in caps:
        return probe_image_cell
    if 'transcription' in caps:
        return probe_transcription_cell
    if 'tts' in caps:
        return probe_tts_cell
    if 'embedding' in caps:
        return probe_embedding_cell
    return None


# probe_fn → probe_surface stamp. Every cell records WHICH surface produced
# its verdict so the frontend can tell a FRESH modality verdict (e.g. an
# image-surface not_found — meaningful, must reach the user) from a STALE
# chat-probe false positive on a non-chat model (must heal on ingest).
# Cells without the stamp (pre-stamp snapshots) or stamped 'chat' are the
# stale kind; 'none' marks a skipped (unprobed) cell.
_PROBE_SURFACE_NAMES = {
    probe_image_cell: 'image',
    probe_transcription_cell: 'transcription',
    probe_tts_cell: 'tts',
    probe_embedding_cell: 'embedding',
}


# Verdicts that warrant a retry (could be a transient blip), versus ones
# that are definitive on the first attempt (no point re-asking).
_PROBE_TRANSIENT = {'rate_limited', 'unavailable', 'error'}
_PROBE_DEFINITIVE = {'unauthorized', 'not_found'}


def probe_cell_multi(base_url, api_key, model_id, extra_headers, timeout,
                     attempts=3, retry_delay=0.8, protocol='openai',
                     probe_fn=None, oauth=''):
    """Probe a cell up to ``attempts`` times to filter out FALSE 429s.

    ``probe_fn`` defaults to :func:`probe_one_cell` (chat surface); the
    modality probes share its signature so the same multi-attempt policy
    drives them unchanged. ``oauth`` is forwarded to the chat surface only —
    modality probes keep the 6-arg signature.
    """
    attempts = max(1, int(attempts))
    fn = probe_fn or probe_one_cell
    last_status, last_detail = 'error', ''
    for i in range(attempts):
        # Call through the module global so tests can patch probe_one_cell.
        if probe_fn is None:
            status, detail = fn(base_url, api_key, model_id, extra_headers,
                                timeout, protocol, oauth=oauth)
        else:
            status, detail = fn(base_url, api_key, model_id, extra_headers,
                                timeout, protocol)
        if status == 'ok':
            note = '' if i == 0 else ' (ok on attempt %d/%d)' % (i + 1, attempts)
            return 'ok', detail + note
        # A session verdict is final — retrying can't conjure a login.
        if status in _PROBE_DEFINITIVE or status in ('not_logged_in', SKIPPED):
            return status, detail
        last_status, last_detail = status, detail
        if i < attempts - 1:
            _time.sleep(retry_delay)
    suffix = ' (%d/%d attempts failed)' % (attempts, attempts) if attempts > 1 else ''
    return last_status, '%.120s%s' % (last_detail, suffix)


# ══════════════════════════════════════════════════════
#  Background task state
# ══════════════════════════════════════════════════════
CELL_PROBE_TASKS: dict = {}
CELL_PROBE_LOCK = threading.Lock()
_PROBE_DISABLE_STATUSES = {'rate_limited', 'unauthorized', 'not_found', 'unavailable'}


def build_probe_work(provider: dict, models: list, api_keys: list) -> list:
    """Build the probe work list: one item per (key × concrete id).

    Each item is ``(key_idx, api_key, root_id, wire_id, caps, base_url,
    protocol)``. The last two are resolved PER MODEL via
    :func:`lib.llm_dispatch.provider_face.resolve_face`, because one account
    can expose several wire faces — probing a Claude cell on the OpenAI face
    of a dual-face gateway returns a false ``not_found`` and the matrix then
    recommends disabling a model that works.

    THE SEAM: the route and the tests both call this, so a regression in the
    resolution is visible to a test rather than hiding behind a re-implemented
    copy of the loop.

    A REFUSED entry (Claude with no anthropic face on a dual-face gateway)
    still gets probed on the provider default: the matrix reports
    reachability, and the refusal itself is surfaced by the dispatcher.
    """
    from lib.llm_dispatch.provider_face import resolve_face

    base_url = provider.get('base_url') or ''
    protocol = (provider.get('protocol') or 'openai') or 'openai'
    work = []
    for key_idx, api_key in enumerate(api_keys or []):
        for m in (models or []):
            root = (m.get('model_id') or '').strip()
            if not root:
                continue
            caps = m.get('capabilities') or []
            face = resolve_face(provider, m)
            cell_url = face.base_url if (face.ok and face.base_url) else base_url
            cell_proto = (face.protocol if face.ok else protocol) or 'openai'
            for mid in [root] + [a for a in (m.get('aliases') or []) if a]:
                work.append((key_idx, api_key, root, mid, caps,
                             cell_url, cell_proto))
    return work


def probe_cache_path(provider_id: str) -> str:
    """Disk path for a provider's persisted probe snapshot."""
    safe = hashlib.sha1((provider_id or '').encode('utf-8')).hexdigest()[:16]
    return _config_path('probe_cache', '%s.json' % safe)


def probe_cell_key(key_idx, model_id) -> str:
    return '%s::%s' % (key_idx, model_id)


def persist_probe_task(task: dict):
    """Atomically write a public (key-free) snapshot of the task to disk."""
    try:
        write_json_atomic(probe_cache_path(task['provider_id']),
                          public_probe_snapshot(task), fsync=False)
    except Exception as e:
        logger.warning('[CellProbe] persist failed for %s: %s',
                       task.get('provider_id'), e)


def _recount_summary(task: dict):
    """Recompute task['summary'] over ALL current cells.

    Scoped probes (only=key/model) seed the task with the persisted snapshot's
    other cells so their result MERGES into the full grid; the summary must
    therefore reflect the merged set from the very first poll, not just the
    cells completed in this run."""
    cells = task['cells']
    n_disable = sum(1 for c in cells.values() if c['recommend_disable'])
    n_skipped = sum(1 for c in cells.values() if c['status'] == SKIPPED)
    task['summary'] = {'ok': len(cells) - n_disable - n_skipped,
                       'disable': n_disable, 'skipped': n_skipped}


def public_probe_snapshot(task: dict) -> dict:
    """The serialisable, secret-free view of a probe task (for poll + disk)."""
    return {
        'provider_id': task['provider_id'],
        'status': task['status'],
        'started_at': task['started_at'],
        'finished_at': task['finished_at'],
        'total': task['total'],
        'done_count': task['done_count'],
        'attempts': task.get('attempts', 1),
        'cells': task['cells'],
        'summary': task['summary'],
        'error': task['error'],
    }


def run_cell_probe_task(task: dict, work: list, timeout: int):
    """Background worker: fan out cell probes, updating + persisting progress."""
    provider_id = task['provider_id']
    base_url = task['_base_url']
    extra_headers = task['_extra_headers']
    protocol = task.get('_protocol', 'openai')
    oauth = task.get('_oauth', '')
    attempts = task.get('attempts', 3)
    if task['cells']:
        _recount_summary(task)
    logger.info('[CellProbe] Started background probe for %s — %d cell(s) this run, '
                '%d seeded, up to %d attempt(s) each (protocol=%s)', provider_id,
                len(work), len(task['cells']), attempts, protocol)

    def _run(item):
        key_idx, api_key, root, mid = item[0], item[1], item[2], item[3]
        caps = item[4] if len(item) > 4 else None
        # Per-cell wire face (account/face separation). A work tuple may carry
        # its own (base_url, protocol) because ONE account can expose several
        # wire faces — e.g. the Meituan gateway serves Claude over
        # /v1/anthropic and everything else over /v1/openai/native with the
        # same keys. Probing a cell on the wrong face returns a false
        # 'not_found'. Older 5-tuples fall back to the task-level values.
        cell_base_url = item[5] if len(item) > 5 and item[5] else base_url
        cell_protocol = item[6] if len(item) > 6 and item[6] else protocol
        cell_attempts = attempts
        cell_timeout = timeout
        probe_fn = None
        surface = 'chat'
        if caps and not _is_chat_model(caps):
            probe_fn = nonchat_probe_fn(caps)
            if probe_fn is None:
                return {
                    'key_idx': key_idx,
                    'model_id': mid,
                    'root_model_id': root,
                    'status': SKIPPED,
                    'detail': 'non-chat model (%s) — no probe surface implemented'
                              % ','.join(caps),
                    'recommend_disable': False,
                    'probe_surface': 'none',
                }
            surface = _PROBE_SURFACE_NAMES[probe_fn]
            if 'image_gen' in caps:
                # One real image is generated per attempt — a billed call, so
                # don't multiply it for transient-filtering; generation also
                # routinely outlives the chat-probe timeout.
                cell_attempts = 1
                cell_timeout = max(timeout, _IMAGE_PROBE_MIN_TIMEOUT)
        # Multi-attempt so a FALSE 429 / transient 5xx doesn't wrongly flag a
        # reachable cell. A single ok on any attempt wins.
        status, detail = probe_cell_multi(cell_base_url, api_key, mid,
                                          extra_headers,
                                          cell_timeout, attempts=cell_attempts,
                                          protocol=cell_protocol, probe_fn=probe_fn,
                                          oauth=oauth)
        return {
            'key_idx': key_idx,
            'model_id': mid,
            'root_model_id': root,
            'status': status,
            'detail': detail,
            'recommend_disable': status in _PROBE_DISABLE_STATUSES,
            'probe_surface': surface,
        }

    last_persist = 0.0
    try:
        workers = min(8, len(work))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='cell-probe') as pool:
            futures = [pool.submit(_run, it) for it in work]
            for fut in as_completed(futures):
                if task.get('_abort'):
                    logger.info('[CellProbe] %s aborted', provider_id)
                    break
                try:
                    cell = fut.result()
                except Exception as e:
                    logger.error('[CellProbe] cell task raised: %s', e, exc_info=True)
                    continue
                with CELL_PROBE_LOCK:
                    task['cells'][probe_cell_key(cell['key_idx'], cell['model_id'])] = cell
                    # Progress counts only THIS RUN's completions — cells
                    # seeded from the disk snapshot (scoped probe) are already
                    # done and must not inflate done/total.
                    task['done_count'] = task.get('done_count', 0) + 1
                    _recount_summary(task)
                # Throttle disk writes: at most every ~1.5s during the run.
                now = _time.monotonic()
                if now - last_persist > 1.5:
                    last_persist = now
                    persist_probe_task(task)
        with CELL_PROBE_LOCK:
            task['status'] = 'done'
            task['finished_at'] = _time.time()
        persist_probe_task(task)
        logger.info('[CellProbe] %s done: %d cells, %d flagged',
                    provider_id, task['done_count'], task['summary']['disable'])
    except Exception as e:
        logger.error('[CellProbe] background worker crashed for %s: %s',
                     provider_id, e, exc_info=True)
        with CELL_PROBE_LOCK:
            task['status'] = 'error'
            task['error'] = str(e)[:300]
            task['finished_at'] = _time.time()
        persist_probe_task(task)


__all__ = [
    'probe_one_cell', 'probe_cell_multi', 'run_cell_probe_task',
    'probe_embedding_cell', 'probe_transcription_cell', 'probe_image_cell',
    'probe_tts_cell', 'nonchat_probe_fn',
    'probe_cache_path', 'probe_cell_key', 'persist_probe_task',
    'build_probe_work',
    'public_probe_snapshot', 'CELL_PROBE_TASKS', 'CELL_PROBE_LOCK', 'SKIPPED',
]
