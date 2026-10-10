"""Provider templates + model catalogue discovery/persistence.

STDLIB-ONLY CONTRACT — see bootstrap_pkg.env_reexec.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from .env_reexec import BASE_DIR

# ══════════════════════════════════════════════════════════
#  Provider templates — reused from Settings UI
# ══════════════════════════════════════════════════════════
# The template catalog is FILE-BACKED and HOT-RELOADED: every HTTP request
# re-reads lib/model_info/data/*.json (package catalog) and
# static/provider_templates/*.json (deployment overrides), merging by key in
# that order, so template edits never require a restart.  The main Settings
# loader (lib/provider_template_recipes.py) merges the same directories in
# the same order, so bootstrap and Settings always offer identical providers.
#
# This Python list is NOT the catalog.  It only retains bootstrap-form
# scaffolding that must exist even when no data file ships: the Anthropic
# stub (overridden by static/provider_templates/anthropic.json, which carries
# the native protocol) and the Custom-endpoint form placeholder.  Never add
# real catalog entries here — author lib/model_info/data/<key>.json instead.
_BUILTIN_PROVIDER_TEMPLATES = [
    {'key': 'anthropic', 'brand': 'claude', 'category': 'official',
     'name': 'Anthropic',
     'base_url': 'https://api.anthropic.com/v1',
     'models': [
         {'model_id': 'fable-5.1',         'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'fable-5',           'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'claude-opus-4-8',   'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'claude-opus-4-7',   'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'claude-sonnet-4-6', 'capabilities': ['text', 'vision', 'thinking']},
         {'model_id': 'claude-haiku-4-5',  'capabilities': ['text', 'vision', 'cheap']},
     ]},
    {'key': 'custom', 'brand': 'custom', 'category': 'custom',
     'name': 'Custom (OpenAI-compatible)',
     'base_url': '',
     'models': [
         {'model_id': '', 'capabilities': ['text']},
     ]},
]
def _load_provider_templates() -> list:
    """Return builtins merged with package-owned and deployment templates.

    Extras from disk override builtins on key conflict (so a deployment
    template replaces an inline stub of the same key). Called on every HTTP request
    so freshly-dropped template files appear without a restart.
    """
    out: list = [dict(t) for t in _BUILTIN_PROVIDER_TEMPLATES]
    seen = {t['key']: i for i, t in enumerate(out)}
    # Same three repo tiers as lib/provider_template_recipes.load_provider_templates:
    # package catalog ← deployment overrides ← release-tiered bundles
    # (docs/SITE_PACKS.md §4; meituan.json lives under bundles/meituan/).
    template_dirs = [
        os.path.join(BASE_DIR, 'lib', 'model_info', 'data'),
        os.path.join(BASE_DIR, 'static', 'provider_templates'),
    ]
    bundles_dir = os.path.join(BASE_DIR, 'bundles')
    if os.path.isdir(bundles_dir):
        template_dirs.extend(
            os.path.join(bundles_dir, bundle, 'provider_templates')
            for bundle in sorted(os.listdir(bundles_dir))
            if os.path.isdir(os.path.join(
                bundles_dir, bundle, 'provider_templates')))
    template_dirs = tuple(template_dirs)
    for extras_dir in template_dirs:
        if not os.path.isdir(extras_dir):
            continue
        for fname in sorted(os.listdir(extras_dir)):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(extras_dir, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    tpl = json.load(f)
            except Exception as e:
                sys.stderr.write(
                    f'[bootstrap] Could not read template {fname}: {e}\n')
                continue
            if not isinstance(tpl, dict) or not tpl.get('key'):
                continue
            recipes = tpl.get('offering_recipes')
            legacy_models = tpl.get('models')
            if not isinstance(recipes, list):
                recipes = legacy_models if isinstance(legacy_models, list) else []
            if not recipes and tpl.get('catalog_mode') != 'discovery':
                continue
            # Internal stdlib projection only: authored files remain v1
            # offering recipes and no legacy provider row is persisted.
            tpl = dict(tpl)
            tpl['models'] = [dict(row) for row in recipes
                             if isinstance(row, dict)]
            key = tpl['key']
            if key in seen:
                out[seen[key]] = tpl   # override builtin
            else:
                out.append(tpl)
                seen[key] = len(out) - 1
    return out
def _bootstrap_infer_capabilities(model_id: str) -> list:
    """Small stdlib-only fallback for live IDs absent from bundled metadata."""
    mid = (model_id or '').lower()
    if any(x in mid for x in ('embedding', 'embed-', 'text-embedding')):
        return ['embedding']
    if any(x in mid for x in ('image-gen', 'image_generation', 'gpt-image',
                              'dall-e', 'imagen')):
        return ['image_gen']
    if any(x in mid for x in ('transcribe', 'whisper', 'speech-to-text')):
        return ['transcription']
    caps = ['text']
    if any(x in mid for x in ('vision', '-vl', 'vl-', 'omni', 'gemini',
                              'claude', 'gpt-4o', 'gpt-4.1', 'gpt-5')):
        caps.append('vision')
    if any(x in mid for x in ('thinking', 'reasoner', 'reasoning', 'deepseek-r1',
                              'qwq', 'o1', 'o3', 'o4', 'gpt-5', 'claude')):
        caps.append('thinking')
    if any(x in mid for x in ('mini', 'nano', 'flash', 'lite', 'haiku',
                              'turbo', 'small')):
        caps.append('cheap')
    return caps
def _bootstrap_discover_models(base_url: str, api_key: str,
                               templates: list | None = None,
                               timeout: int = 10) -> list:
    """Fetch an authenticated OpenAI-compatible catalogue using stdlib only.

    Bootstrap exists precisely for environments whose third-party packages
    are broken, so importing the application's normal discovery stack here is
    not an option. Fail-soft: callers retain the selected template model when
    this endpoint is unavailable.
    """
    if not base_url or not api_key:
        return []
    url = base_url.rstrip('/') + '/models'
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('model endpoint must be an http(s) URL with a hostname')
        allow_hosts = {
            h.strip().lower()
            for h in os.environ.get('TOFU_BYO_ALLOW_HOSTS', '').split(',')
            if h.strip()
        }
        if parsed.hostname.lower() not in allow_hosts:
            infos = socket.getaddrinfo(
                parsed.hostname, parsed.port or None, proto=socket.IPPROTO_TCP)
            if not infos:
                raise ValueError('model endpoint DNS returned no addresses')
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                ip = getattr(ip, 'ipv4_mapped', None) or ip
                blocked = (ip.is_link_local or ip.is_multicast
                           or ip.is_reserved or ip.is_unspecified)
                block_loopback = os.environ.get(
                    'TOFU_BYO_BLOCK_LOOPBACK', '').strip().lower() in (
                        '1', 'true', 'yes', 'on')
                block_private = os.environ.get(
                    'TOFU_BYO_BLOCK_PRIVATE', '').strip().lower() in (
                        '1', 'true', 'yes', 'on')
                if (blocked or (block_loopback and ip.is_loopback)
                        or (block_private and ip.is_private
                            and not ip.is_loopback)):
                    raise ValueError(
                        f'model endpoint resolves to blocked address {ip}')
    except (OSError, ValueError) as e:
        sys.stderr.write(f'[bootstrap] model discovery blocked for {base_url}: {e}\n')
        return []

    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + api_key,
        'User-Agent': 'Tofu-Bootstrap/1.0',
        'Accept': 'application/json',
    })
    try:
        # Do not follow redirects: a public-looking provider URL redirecting
        # to a link-local metadata service would bypass the check above.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=timeout) as resp:
            if getattr(resp, 'status', 200) >= 400:
                return []
            raw = resp.read(4 * 1024 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        sys.stderr.write(f'[bootstrap] model discovery failed for {base_url}: {e}\n')
        return []
    if len(raw) > 4 * 1024 * 1024:
        sys.stderr.write('[bootstrap] model discovery response exceeded 4 MiB\n')
        return []
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        sys.stderr.write(f'[bootstrap] invalid model catalogue JSON: {e}\n')
        return []
    rows = data.get('data') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []

    metadata = {}
    catalog_templates = (templates if templates is not None
                         else _load_provider_templates())
    norm_base = base_url.rstrip('/')
    matching_templates = [
        tpl for tpl in catalog_templates
        if str(tpl.get('base_url') or '').rstrip('/') == norm_base
    ]
    for tpl in matching_templates:
        for model in (tpl.get('models') or []):
            if isinstance(model, dict) and model.get('model_id'):
                metadata.setdefault(model['model_id'], model)
    result = []
    seen = set()
    for raw_model in rows:
        if not isinstance(raw_model, dict):
            continue
        model_id = str(raw_model.get('id') or '').strip()
        if (not model_id or model_id in seen
                or model_id.startswith(('system-', 'ft:', 'ft-'))):
            continue
        seen.add(model_id)
        known = metadata.get(model_id) or {}
        caps = list(known.get('capabilities') or
                    _bootstrap_infer_capabilities(model_id))
        result.append({
            'model_id': model_id,
            'aliases': list(known.get('aliases') or []),
            'capabilities': caps,
            'rpm': int(known.get('rpm') or 30),
            'cost': float(known.get('cost') or 0.01),
            'thinking_default': 'thinking' in caps,
            'catalog_managed': True,
            'catalog_source': 'provider',
        })
    result.sort(key=lambda m: m['model_id'].casefold())
    return result
def _bootstrap_choose_model(models: list, requested: str = '') -> str:
    """Keep a valid selection; otherwise choose a cheap chat model."""
    chat = [m for m in (models or [])
            if 'text' in set(m.get('capabilities') or [])]
    ids = {m.get('model_id') for m in chat}
    if requested in ids:
        return requested
    for model in chat:
        if 'cheap' in set(model.get('capabilities') or []):
            return model['model_id']
    return chat[0]['model_id'] if chat else ''
def _bootstrap_template_models(base_url: str, templates: list) -> list:
    """Convert the matching bundled template into persisted bootstrap rows."""
    norm = (base_url or '').rstrip('/')
    template = next((tpl for tpl in (templates or [])
                     if str(tpl.get('base_url') or '').rstrip('/') == norm), {})
    result = []
    for raw in (template.get('models') or []):
        if not isinstance(raw, dict) or not raw.get('model_id'):
            continue
        row = dict(raw)
        caps = list(row.get('capabilities') or
                    _bootstrap_infer_capabilities(row['model_id']))
        row['capabilities'] = caps
        row.setdefault('aliases', [])
        row.setdefault('rpm', 30)
        row.setdefault('cost', 0.01)
        row.setdefault('thinking_default', 'thinking' in caps)
        row['catalog_managed'] = True
        row['catalog_source'] = 'template'
        result.append(row)
    return result
def _bootstrap_data_root() -> str:
    """Stdlib twin of lib.runtime_paths.data_root for pre-dependency setup."""
    explicit = os.environ.get('TOFU_DATA_DIR', '').strip()
    if explicit:
        explicit = os.path.abspath(os.path.expanduser(explicit))
        return (explicit if os.path.basename(explicit) == 'data'
                else os.path.join(explicit, 'data'))

    def _per_user_data() -> str:
        if sys.platform.startswith('win'):
            base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
            return os.path.join(base, 'Tofu', 'data')
        if sys.platform == 'darwin':
            return os.path.join(os.path.expanduser('~'), 'Library',
                                'Application Support', 'Tofu', 'data')
        base = os.environ.get('XDG_DATA_HOME') or os.path.join(
            os.path.expanduser('~'), '.local', 'share')
        return os.path.join(base, 'Tofu', 'data')

    if getattr(sys, 'frozen', False):
        exe_base = os.path.dirname(sys.executable)
        try:
            os.makedirs(exe_base, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix='.tofu-write-probe-', dir=exe_base)
            os.close(fd)
            os.unlink(probe)
            return os.path.join(exe_base, 'data')
        except OSError:
            return _per_user_data()

    intree = os.path.join(BASE_DIR, 'data')
    layout = os.environ.get('TOFU_DATA_LAYOUT', 'auto').strip().lower()
    if layout not in ('auto', 'intree', 'xdg'):
        layout = 'auto'
    try:
        populated = os.path.isdir(intree) and bool(os.listdir(intree))
    except OSError:
        populated = False
    if layout == 'intree' or (layout == 'auto' and populated):
        return intree
    return _per_user_data()
def _bootstrap_persist_provider(base_url: str, api_key: str, models: list,
                                templates: list | None = None,
                                default_model: str = '') -> None:
    """Stage a secret-free provider draft for model-routing v2 startup.

    The repair UI already writes the credential to ``.env``. Duplicating it
    inside legacy ``server_config.providers`` would leave plaintext routing
    state outside the owner repository and cannot update an already-active v2
    authority. This file carries only transport/model facts plus the name of
    the credential environment variable; the full application consumes and
    deletes it after the authenticated storage sidecar is available.
    """
    if not models:
        return
    templates = (templates if templates is not None
                 else _load_provider_templates())
    norm = base_url.rstrip('/')
    template = next((t for t in templates
                     if str(t.get('base_url') or '').rstrip('/') == norm), {})
    config_path = os.path.join(_bootstrap_data_root(), 'config',
                               'server_config.json')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if not isinstance(config, dict):
            config = {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        config = {}
    pending_path = os.path.join(
        _bootstrap_data_root(), 'config', '.bootstrap-provider-pending.json')
    try:
        with open(pending_path, 'r', encoding='utf-8') as f:
            previous_pending = json.load(f)
        if not isinstance(previous_pending, dict):
            previous_pending = {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        previous_pending = {}

    catalog_models = [dict(row) for row in models[:1024]
                      if isinstance(row, dict) and row.get('model_id')]
    live_ids = {m.get('model_id') for m in catalog_models if isinstance(m, dict)}
    previous_models = (
        previous_pending.get('models')
        if str(previous_pending.get('base_url') or '').rstrip('/') == norm
        else [])
    for old_model in (previous_models or []):
        if (isinstance(old_model, dict) and old_model.get('model_id')
                and old_model.get('catalog_pinned') is True
                and old_model['model_id'] not in live_ids):
            catalog_models.append(dict(old_model))
            live_ids.add(old_model['model_id'])

    protocol = str(template.get('protocol') or 'openai').strip().lower()
    if protocol in ('responses', 'openai-responses'):
        protocol = 'openai_responses'
    pending = {
        'contract_version': 'tofu.bootstrap-provider-stage/v1',
        'name': template.get('name') or 'Bootstrap Provider',
        'brand': template.get('brand') or 'generic',
        'base_url': base_url,
        'protocol': protocol,
        'models': catalog_models,
        'credential_env': 'LLM_API_KEYS',
        'default_model': default_model,
    }
    if default_model:
        config.setdefault('presets', {})['opus'] = default_model
        config.setdefault('models', {})['LLM_MODEL'] = default_model
        config.setdefault('model_defaults', {})['default_model'] = default_model

    parent = os.path.dirname(config_path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix='.bootstrap-config-', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass

    fd, tmp_path = tempfile.mkstemp(
        prefix='.bootstrap-provider-', suffix='.tmp', dir=parent)
    try:
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, pending_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
