#!/usr/bin/env python3
"""benchmark_new_models.py — Probe + RPM + vision + thinking test for new models.

Tests:
  • gemini-3.1-pro-preview        (text, vision, thinking via reasoning_content)
  • MiniMax-M2.7                  (text, always-on inline <think> tags — no vision, no API thinking toggle)
  • gemini-3.1-flash-image-preview (image generation — extreme rate limits)
  • text-embedding-3-large         (embedding)
  • text-embedding-3-small         (embedding)
  • text-embedding-v4              (embedding)

Usage:
    python debug/benchmark_new_models.py
"""

import json, os, sys, time, statistics, base64, struct, zlib, logging
from datetime import datetime
from threading import Thread, Event, Lock
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from lib import LLM_API_KEYS, LLM_BASE_URL

CHAT_URL = f'{LLM_BASE_URL}/chat/completions'
EMBED_URL = f'{LLM_BASE_URL}/embeddings'
KEYS = {f'key_{i}': k for i, k in enumerate(LLM_API_KEYS)}

NEW_MODELS = [
    {'model': 'gemini-3.1-pro-preview',         'tags': ['text', 'vision', 'thinking']},
    {'model': 'MiniMax-M2.7',                    'tags': ['text']},  # no vision, inline <think> always-on (not API-controllable)
    {'model': 'gemini-3.1-flash-image-preview',  'tags': ['text', 'image_gen']},  # severe rate limits (~2 RPM key_0, dead key_1)
]

EMBEDDING_MODELS = [
    'text-embedding-3-large',
    'text-embedding-3-small',
    'text-embedding-v4',
]


def _headers(api_key):
    return {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}

def _no_proxy():
    return {'http': None, 'https': None}

def _make_test_png_b64():
    width, height = 2, 2
    raw = b''
    raw += b'\x00' + b'\xff\x00\x00\xff' + b'\x00\xff\x00\xff'
    raw += b'\x00' + b'\x00\x00\xff\xff' + b'\xff\xff\xff\xff'
    def chunk(ctype, data):
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    png = sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
    return base64.b64encode(png).decode()


# ═══════════════════════════════════════════════════════════
#  1) Probe
# ═══════════════════════════════════════════════════════════

def probe_model(key_name, api_key, model, timeout=60):
    print(f'  🔍 Probe: [{key_name}] {model} ...', end=' ', flush=True)
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'Say hello in one word.'}],
        'max_tokens': 50,
        'temperature': 0,
    }
    t0 = time.time()
    try:
        resp = requests.post(CHAT_URL, headers=_headers(api_key), json=body,
                             timeout=timeout, proxies=_no_proxy())
        lat = (time.time() - t0) * 1000
        if resp.status_code == 200:
            data = resp.json()
            content = ''
            choices = data.get('choices', [])
            if choices:
                content = (choices[0].get('message', {}).get('content', '') or '')[:80]
            print(f'✅ {lat:.0f}ms "{content}"')
            return {'alive': True, 'latency_ms': round(lat, 1), 'preview': content}
        elif resp.status_code == 429:
            print(f'⚠️ 429 (model exists but rate-limited)')
            return {'alive': True, 'latency_ms': round(lat, 1), 'rate_limited': True}
        else:
            print(f'❌ HTTP {resp.status_code}: {resp.text[:200]}')
            return {'alive': False, 'error': f'HTTP {resp.status_code}', 'detail': resp.text[:300]}
    except Exception as e:
        lat = (time.time() - t0) * 1000
        print(f'❌ {e}')
        return {'alive': False, 'error': str(e)}


# ═══════════════════════════════════════════════════════════
#  2) RPM Test
# ═══════════════════════════════════════════════════════════

def test_rpm(key_name, api_key, model, concurrency=6, duration_sec=30):
    print(f'\n  🔄 RPM: [{key_name}] {model} (c={concurrency}, {duration_sec}s)')
    stop = Event()
    lock = Lock()
    c = {'ok': 0, '429': 0, 'err': 0}
    lats = []

    def worker():
        while not stop.is_set():
            body = {
                'model': model,
                'messages': [{'role': 'user', 'content': 'Count to 3.'}],
                'max_tokens': 20, 'temperature': 0,
            }
            t0 = time.time()
            try:
                r = requests.post(CHAT_URL, headers=_headers(api_key), json=body,
                                  timeout=30, proxies=_no_proxy())
                lat = (time.time() - t0) * 1000
                with lock:
                    if r.status_code == 200:
                        c['ok'] += 1
                        lats.append(lat)
                    elif r.status_code == 429:
                        c['429'] += 1
                        time.sleep(1.0)
                    else:
                        c['err'] += 1
            except Exception as exc:
                logger.debug('Request error: %s', exc)
                with lock:
                    c['err'] += 1
            time.sleep(0.05)

    threads = [Thread(target=worker, daemon=True) for _ in range(concurrency)]
    t_start = time.time()
    for t in threads: t.start()
    time.sleep(duration_sec)
    stop.set()
    for t in threads: t.join(timeout=5)
    elapsed = time.time() - t_start

    rpm = (c['ok'] / elapsed) * 60 if elapsed > 0 else 0
    result = {
        'rpm_effective': round(rpm, 1),
        'success': c['ok'], '429s': c['429'], 'errors': c['err'],
        'duration_sec': round(elapsed, 1),
    }
    if lats:
        result['avg_lat_ms'] = round(statistics.mean(lats), 1)
        result['p50_lat_ms'] = round(statistics.median(lats), 1)
    print(f'     → RPM≈{rpm:.0f}  ok={c["ok"]} 429s={c["429"]} err={c["err"]}')
    return result


# ═══════════════════════════════════════════════════════════
#  3) Vision Test
# ═══════════════════════════════════════════════════════════

def test_vision(key_name, api_key, model, timeout=60):
    print(f'  👁️  Vision: [{key_name}] {model} ...', end=' ', flush=True)
    tiny_png = _make_test_png_b64()
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'What colors do you see in this image? Answer briefly.'},
            {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{tiny_png}'}},
        ]}],
        'max_tokens': 100, 'temperature': 0,
    }
    t0 = time.time()
    try:
        resp = requests.post(CHAT_URL, headers=_headers(api_key), json=body,
                             timeout=timeout, proxies=_no_proxy())
        lat = (time.time() - t0) * 1000
        if resp.status_code == 200:
            data = resp.json()
            content = (data.get('choices', [{}])[0].get('message', {}).get('content', '') or '')[:150]
            ok = bool(content.strip())
            print(f'{"✅" if ok else "⚠️"} {lat:.0f}ms "{content[:80]}"')
            return {'vision_ok': ok, 'latency_ms': round(lat, 1), 'response': content}
        else:
            print(f'❌ HTTP {resp.status_code}: {resp.text[:200]}')
            return {'vision_ok': False, 'error': f'HTTP {resp.status_code}'}
    except Exception as e:
        print(f'❌ {e}')
        return {'vision_ok': False, 'error': str(e)}


# ═══════════════════════════════════════════════════════════
#  4) Thinking Test (streaming — check for reasoning_content or <think>)
# ═══════════════════════════════════════════════════════════

def test_thinking(key_name, api_key, model, timeout=90):
    print(f'  🧠 Thinking: [{key_name}] {model} ...', end=' ', flush=True)
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'What is 17 * 23? Show your reasoning step by step.'}],
        'max_tokens': 1024,
        'temperature': 1.0,
        'stream': True,
    }
    t0 = time.time()
    thinking_text = ''
    content_text = ''
    has_reasoning_field = False
    has_think_tags = False

    try:
        resp = requests.post(CHAT_URL, headers=_headers(api_key), json=body,
                             timeout=timeout, stream=True, proxies=_no_proxy())
        if resp.status_code != 200:
            print(f'❌ HTTP {resp.status_code}')
            return {'thinking_ok': False, 'error': f'HTTP {resp.status_code}'}

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith('data: '): continue
            payload = line[6:].strip()
            if payload == '[DONE]': break
            try:
                chunk = json.loads(payload)
                delta = chunk.get('choices', [{}])[0].get('delta', {})
                # Check for reasoning_content field
                rc = delta.get('reasoning_content') or delta.get('thinking', '')
                if rc:
                    thinking_text += rc
                    has_reasoning_field = True
                cd = delta.get('content', '')
                if cd:
                    content_text += cd
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                logger.debug('Skipping unparseable SSE chunk: %s', exc)
        resp.close()

        # Check for <think> tags in content
        if '<think>' in content_text:
            has_think_tags = True
            import re
            think_match = re.search(r'<think>(.*?)</think>', content_text, re.DOTALL)
            if think_match:
                thinking_text = think_match.group(1)[:300]

        lat = (time.time() - t0) * 1000
        ok = has_reasoning_field or has_think_tags
        method = 'reasoning_content' if has_reasoning_field else ('think_tags' if has_think_tags else 'none')
        print(f'{"✅" if ok else "⚠️"} {lat:.0f}ms method={method} '
              f'think={len(thinking_text)}c content={len(content_text)}c')
        return {
            'thinking_ok': ok,
            'method': method,
            'thinking_len': len(thinking_text),
            'content_len': len(content_text),
            'latency_ms': round(lat, 1),
            'thinking_preview': thinking_text[:200],
            'content_preview': content_text[:200],
        }
    except Exception as e:
        print(f'❌ {e}')
        return {'thinking_ok': False, 'error': str(e)}


# ═══════════════════════════════════════════════════════════
#  5) Image Generation Test (gemini-3.1-flash-image-preview)
# ═══════════════════════════════════════════════════════════

def test_image_gen(key_name, api_key, model, timeout=120):
    print(f'  🎨 Image Gen: [{key_name}] {model} ...', end=' ', flush=True)

    # Method 1: Standard chat completion asking for image generation
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'Generate a simple image of a red circle on a white background.'}],
        'max_tokens': 4096,
        'temperature': 1.0,
    }
    t0 = time.time()
    try:
        resp = requests.post(CHAT_URL, headers=_headers(api_key), json=body,
                             timeout=timeout, proxies=_no_proxy())
        lat = (time.time() - t0) * 1000
        if resp.status_code == 200:
            data = resp.json()
            msg = data.get('choices', [{}])[0].get('message', {})
            content = msg.get('content', '')

            # Check if response contains base64 image data
            has_image = False
            image_data_len = 0

            # Check for multipart content with image blocks
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get('type') == 'image_url':
                            has_image = True
                            url = part.get('image_url', {}).get('url', '')
                            image_data_len = len(url)
                        elif part.get('type') == 'image':
                            has_image = True
                            image_data_len = len(str(part))
            elif isinstance(content, str):
                # Check for inline base64 data
                if 'data:image' in content or len(content) > 5000:
                    has_image = True
                    image_data_len = len(content)

            usage = data.get('usage', {})
            print(f'{"✅" if has_image else "⚠️ (text only)"} {lat:.0f}ms '
                  f'has_image={has_image} content_len={len(str(content))} '
                  f'usage={json.dumps(usage)}')
            return {
                'image_gen_ok': True,
                'has_image_data': has_image,
                'image_data_len': image_data_len,
                'content_type': type(content).__name__,
                'content_preview': str(content)[:300],
                'latency_ms': round(lat, 1),
                'usage': usage,
            }
        elif resp.status_code == 429:
            print(f'⚠️ 429 rate-limited')
            return {'image_gen_ok': False, 'rate_limited': True, 'error': 'HTTP 429'}
        else:
            print(f'❌ HTTP {resp.status_code}: {resp.text[:200]}')
            return {'image_gen_ok': False, 'error': f'HTTP {resp.status_code}', 'detail': resp.text[:300]}
    except Exception as e:
        print(f'❌ {e}')
        return {'image_gen_ok': False, 'error': str(e)}


# ═══════════════════════════════════════════════════════════
#  6) Embedding Test
# ═══════════════════════════════════════════════════════════

def test_embedding(key_name, api_key, model, timeout=30):
    print(f'  📐 Embedding: [{key_name}] {model} ...', end=' ', flush=True)
    body = {
        'model': model,
        'input': 'Hello, world! This is a test of the embedding model.',
    }
    t0 = time.time()
    try:
        resp = requests.post(EMBED_URL, headers=_headers(api_key), json=body,
                             timeout=timeout, proxies=_no_proxy())
        lat = (time.time() - t0) * 1000
        if resp.status_code == 200:
            data = resp.json()
            embeddings = data.get('data', [])
            if embeddings:
                vec = embeddings[0].get('embedding', [])
                dim = len(vec)
                usage = data.get('usage', {})
                print(f'✅ {lat:.0f}ms dim={dim} usage={json.dumps(usage)}')
                return {
                    'embedding_ok': True, 'latency_ms': round(lat, 1),
                    'dimensions': dim, 'usage': usage,
                }
            else:
                print(f'⚠️ No embedding data')
                return {'embedding_ok': False, 'error': 'no embedding data'}
        elif resp.status_code == 429:
            print(f'⚠️ 429 rate-limited')
            return {'embedding_ok': False, 'rate_limited': True}
        else:
            print(f'❌ HTTP {resp.status_code}: {resp.text[:200]}')
            return {'embedding_ok': False, 'error': f'HTTP {resp.status_code}', 'detail': resp.text[:300]}
    except Exception as e:
        print(f'❌ {e}')
        return {'embedding_ok': False, 'error': str(e)}


def test_embedding_rpm(key_name, api_key, model, concurrency=4, duration_sec=15):
    """Quick RPM test for embedding models."""
    print(f'  🔄 Embed RPM: [{key_name}] {model} (c={concurrency}, {duration_sec}s)')
    stop = Event()
    lock = Lock()
    c = {'ok': 0, '429': 0, 'err': 0}

    def worker():
        while not stop.is_set():
            body = {'model': model, 'input': 'test embedding performance'}
            try:
                r = requests.post(EMBED_URL, headers=_headers(api_key), json=body,
                                  timeout=15, proxies=_no_proxy())
                with lock:
                    if r.status_code == 200: c['ok'] += 1
                    elif r.status_code == 429: c['429'] += 1; time.sleep(0.5)
                    else: c['err'] += 1
            except Exception as exc:
                logger.debug('Embedding request error: %s', exc)
                with lock: c['err'] += 1
            time.sleep(0.02)

    threads = [Thread(target=worker, daemon=True) for _ in range(concurrency)]
    t_start = time.time()
    for t in threads: t.start()
    time.sleep(duration_sec)
    stop.set()
    for t in threads: t.join(timeout=5)
    elapsed = time.time() - t_start

    rpm = (c['ok'] / elapsed) * 60 if elapsed > 0 else 0
    print(f'     → RPM≈{rpm:.0f}  ok={c["ok"]} 429s={c["429"]} err={c["err"]}')
    return {'rpm_effective': round(rpm, 1), 'success': c['ok'], '429s': c['429'], 'errors': c['err']}


# ═══════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════

def main():
    print(f'🚀 New Models Benchmark — {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'   Base URL: {LLM_BASE_URL}')
    print(f'   Keys: {", ".join(f"{k}=...{v[-4:]}" for k, v in KEYS.items() if v)}')

    results = {'timestamp': datetime.now().isoformat(), 'chat_models': {}, 'embedding_models': {}}
    active_keys = {k: v for k, v in KEYS.items() if v}

    # ═══ Chat Models ═══
    print(f'\n{"="*70}')
    print(f'  CHAT MODELS: {len(NEW_MODELS)} models × {len(active_keys)} keys')
    print(f'{"="*70}')

    alive_pairs = {}  # key -> set of alive models

    # Phase 1: Probe
    print(f'\n--- Phase 1: PROBE ---')
    for key_name, api_key in active_keys.items():
        for minfo in NEW_MODELS:
            model = minfo['model']
            ek = f'{key_name}:{model}'
            result = probe_model(key_name, api_key, model)
            results['chat_models'][ek] = {'probe': result, 'tags': minfo['tags']}
            if result.get('alive'):
                alive_pairs.setdefault(key_name, set()).add(model)

    # Phase 2: RPM (only for alive pairs)
    print(f'\n--- Phase 2: RPM ---')
    for key_name, api_key in active_keys.items():
        for minfo in NEW_MODELS:
            model = minfo['model']
            if model not in alive_pairs.get(key_name, set()):
                continue
            ek = f'{key_name}:{model}'
            result = test_rpm(key_name, api_key, model, concurrency=6, duration_sec=30)
            results['chat_models'][ek]['rpm'] = result

    # Phase 3: Vision
    print(f'\n--- Phase 3: VISION ---')
    for key_name, api_key in active_keys.items():
        for minfo in NEW_MODELS:
            model = minfo['model']
            if 'vision' not in minfo['tags']:
                continue
            if model not in alive_pairs.get(key_name, set()):
                continue
            ek = f'{key_name}:{model}'
            result = test_vision(key_name, api_key, model)
            results['chat_models'][ek]['vision'] = result

    # Phase 4: Thinking
    print(f'\n--- Phase 4: THINKING ---')
    for key_name, api_key in active_keys.items():
        for minfo in NEW_MODELS:
            model = minfo['model']
            if 'thinking' not in minfo['tags']:
                continue
            if model not in alive_pairs.get(key_name, set()):
                continue
            ek = f'{key_name}:{model}'
            result = test_thinking(key_name, api_key, model)
            results['chat_models'][ek]['thinking'] = result

    # Phase 5: Image Generation (only for image_gen models)
    print(f'\n--- Phase 5: IMAGE GENERATION ---')
    for key_name, api_key in active_keys.items():
        for minfo in NEW_MODELS:
            model = minfo['model']
            if 'image_gen' not in minfo['tags']:
                continue
            if model not in alive_pairs.get(key_name, set()):
                continue
            ek = f'{key_name}:{model}'
            result = test_image_gen(key_name, api_key, model)
            results['chat_models'][ek]['image_gen'] = result

    # ═══ Embedding Models ═══
    print(f'\n{"="*70}')
    print(f'  EMBEDDING MODELS: {len(EMBEDDING_MODELS)} models × {len(active_keys)} keys')
    print(f'{"="*70}')

    for key_name, api_key in active_keys.items():
        for model in EMBEDDING_MODELS:
            ek = f'{key_name}:{model}'
            result = test_embedding(key_name, api_key, model)
            results['embedding_models'][ek] = {'probe': result}
            if result.get('embedding_ok'):
                rpm_result = test_embedding_rpm(key_name, api_key, model)
                results['embedding_models'][ek]['rpm'] = rpm_result

    # ═══ Save & Summary ═══
    output = os.path.join(os.path.dirname(__file__), 'benchmark_new_models.json')
    with open(output, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f'\n💾 Saved to {output}')

    # Summary
    print(f'\n{"="*100}')
    print(f'  {"Key":<10} {"Model":<38} {"Alive":>5} {"RPM":>6} {"Vision":>6} {"Think":>6} {"ImgGen":>6}')
    print(f'{"─"*100}')
    for ek, entry in sorted(results['chat_models'].items()):
        parts = ek.split(':', 1)
        key, model = parts[0], parts[1]
        probe = entry.get('probe', {})
        alive = '✅' if probe.get('alive') else '❌'
        rpm_data = entry.get('rpm', {})
        rpm_str = f'{rpm_data.get("rpm_effective", 0):.0f}' if rpm_data else '—'
        vis = entry.get('vision', {})
        vis_str = '✅' if vis.get('vision_ok') else ('❌' if vis else '—')
        think = entry.get('thinking', {})
        think_str = think.get('method', '—') if think.get('thinking_ok') else ('❌' if think else '—')
        img = entry.get('image_gen', {})
        img_str = '✅' if img.get('image_gen_ok') else ('❌' if img else '—')
        print(f'  {key:<10} {model:<38} {alive:>5} {rpm_str:>6} {vis_str:>6} {think_str:>16} {img_str:>6}')

    print(f'\n  {"Key":<10} {"Model":<38} {"OK":>5} {"Dim":>6} {"RPM":>6}')
    print(f'{"─"*80}')
    for ek, entry in sorted(results['embedding_models'].items()):
        parts = ek.split(':', 1)
        key, model = parts[0], parts[1]
        probe = entry.get('probe', {})
        ok = '✅' if probe.get('embedding_ok') else '❌'
        dim = str(probe.get('dimensions', '—'))
        rpm_data = entry.get('rpm', {})
        rpm_str = f'{rpm_data.get("rpm_effective", 0):.0f}' if rpm_data else '—'
        print(f'  {key:<10} {model:<38} {ok:>5} {dim:>6} {rpm_str:>6}')

    print(f'{"="*100}')
    print(f'✅ Benchmark complete!')


if __name__ == '__main__':
    main()
