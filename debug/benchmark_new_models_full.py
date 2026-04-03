#!/usr/bin/env python3
"""Full benchmark for new models: gemini-3.1-pro-preview, MiniMax-M2.7, embeddings.

Tests:
  1. Probe (alive check)
  2. RPM quota (burst 4-concurrent for 20s)
  3. Vision (multimodal image input)
  4. Thinking toggle (on/off)
  5. Embedding models
"""
import base64, json, os, sys, time, threading, statistics
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from lib import LLM_API_KEYS, LLM_BASE_URL

CHAT_URL = f'{LLM_BASE_URL}/chat/completions'
EMBED_URL = f'{LLM_BASE_URL}/embeddings'
KEYS = {f'key_{i}': k for i, k in enumerate(LLM_API_KEYS)}
NP = {'http': None, 'https': None}

CHAT_MODELS = ['gemini-3.1-pro-preview', 'MiniMax-M2.7']
EMBED_MODELS = ['text-embedding-3-large', 'text-embedding-3-small', 'text-embedding-v4']

# 1x1 red PNG for vision test
RED_1x1 = (
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4'
    'nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=='
)

results = {}

def h(api_key):
    return {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}

# ═══════════════════════════════════════════════
#  1) Probe
# ═══════════════════════════════════════════════
def probe(key_name, api_key, model):
    print(f'  🔍 Probe: [{key_name}] {model} ...', end=' ', flush=True)
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'Say "hello" and nothing else.'}],
        'max_tokens': 64,
        'stream': False,
    }
    t0 = time.time()
    try:
        r = requests.post(CHAT_URL, headers=h(api_key), json=body, proxies=NP, timeout=60)
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200:
            data = r.json()
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            usage = data.get('usage', {})
            print(f'✅ {elapsed:.0f}ms | "{content[:50]}" | usage={json.dumps(usage)}')
            return {'alive': True, 'latency_ms': round(elapsed), 'content': content[:100], 'usage': usage}
        else:
            msg = r.text[:200]
            print(f'❌ HTTP {r.status_code}: {msg}')
            return {'alive': False, 'error': f'HTTP {r.status_code}: {msg}'}
    except Exception as e:
        print(f'❌ {e}')
        return {'alive': False, 'error': str(e)}


# ═══════════════════════════════════════════════
#  2) RPM (burst 4-concurrent for 20s)
# ═══════════════════════════════════════════════
def rpm_test(key_name, api_key, model, duration=20, concurrency=4):
    print(f'  ⏱️  RPM: [{key_name}] {model} ({concurrency}×{duration}s) ...', end=' ', flush=True)
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'Hi'}],
        'max_tokens': 16,
        'stream': False,
    }
    lock = threading.Lock()
    ok_times, err429, err_other = [], 0, 0
    stop_at = time.time() + duration

    def worker():
        nonlocal err429, err_other
        while time.time() < stop_at:
            t0 = time.time()
            try:
                r = requests.post(CHAT_URL, headers=h(api_key), json=body, proxies=NP, timeout=30)
                lat = (time.time() - t0) * 1000
                if r.status_code == 200:
                    with lock:
                        ok_times.append(lat)
                elif r.status_code == 429:
                    with lock:
                        err429 += 1
                    time.sleep(0.5)
                else:
                    with lock:
                        err_other += 1
            except Exception:
                with lock:
                    err_other += 1

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    t_start = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    wall = time.time() - t_start

    total = len(ok_times) + err429 + err_other
    rpm_raw = len(ok_times) / wall * 60 if wall > 0 else 0
    r429_ratio = err429 / total if total else 0

    info = {
        'rpm_raw': round(rpm_raw, 1),
        'rpm_effective': round(rpm_raw, 1),
        'success_count': len(ok_times),
        'error_429_count': err429,
        'error_other_count': err_other,
        'total_requests': total,
        'duration_sec': round(wall, 1),
        'concurrency': concurrency,
        '_429_ratio': round(r429_ratio, 3),
    }
    if ok_times:
        ok_sorted = sorted(ok_times)
        info['avg_latency_ms'] = round(statistics.mean(ok_times), 1)
        info['p50_latency_ms'] = round(ok_sorted[len(ok_sorted)//2], 1)
        info['p95_latency_ms'] = round(ok_sorted[int(len(ok_sorted)*0.95)], 1)
    print(f'rpm={rpm_raw:.0f} ok={len(ok_times)} 429={err429} other={err_other}')
    return info


# ═══════════════════════════════════════════════
#  3) Vision
# ═══════════════════════════════════════════════
def vision_test(key_name, api_key, model):
    print(f'  👁️  Vision: [{key_name}] {model} ...', end=' ', flush=True)
    body = {
        'model': model,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'What color is this pixel? Reply with just the color name.'},
                {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{RED_1x1}'}},
            ],
        }],
        'max_tokens': 64,
        'stream': False,
    }
    t0 = time.time()
    try:
        r = requests.post(CHAT_URL, headers=h(api_key), json=body, proxies=NP, timeout=60)
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200:
            data = r.json()
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            ok = 'red' in content.lower()
            print(f'{"✅" if ok else "⚠️"} {elapsed:.0f}ms | "{content[:60]}"')
            return {'vision_ok': ok, 'latency_ms': round(elapsed), 'response': content[:100]}
        else:
            msg = r.text[:200]
            print(f'❌ HTTP {r.status_code}: {msg}')
            return {'vision_ok': False, 'error': f'HTTP {r.status_code}: {msg}'}
    except Exception as e:
        print(f'❌ {e}')
        return {'vision_ok': False, 'error': str(e)}


# ═══════════════════════════════════════════════
#  4) Thinking Toggle
# ═══════════════════════════════════════════════
def thinking_test(key_name, api_key, model):
    print(f'  🧠 Thinking: [{key_name}] {model}')
    result = {}

    # 4a. thinking ON
    print(f'    → ON ...', end=' ', flush=True)
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'What is 15 * 23? Just the number.'}],
        'max_tokens': 4096,
        'stream': False,
    }
    # Add thinking budget
    if 'gemini' in model.lower():
        body['extra_body'] = {'google': {'thinking_config': {'thinking_budget': 1024}}}
    elif 'minimax' in model.lower():
        # MiniMax uses <think> tags automatically, just enable high tokens
        pass
    t0 = time.time()
    try:
        r = requests.post(CHAT_URL, headers=h(api_key), json=body, proxies=NP, timeout=90)
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200:
            data = r.json()
            msg = data.get('choices', [{}])[0].get('message', {})
            content = msg.get('content', '')
            reasoning = msg.get('reasoning_content', '')
            usage = data.get('usage', {})
            ctd = usage.get('completion_tokens_details', {})
            reasoning_tokens = ctd.get('reasoning_tokens', 0)
            has_think_tags = '<think>' in content
            print(f'✅ {elapsed:.0f}ms | reasoning_tokens={reasoning_tokens} | '
                  f'has_reasoning_content={bool(reasoning)} | has_think_tags={has_think_tags} | '
                  f'content="{content[:60]}"')
            result['on'] = {
                'ok': True,
                'reasoning_tokens': reasoning_tokens,
                'has_reasoning_content': bool(reasoning),
                'has_think_tags': has_think_tags,
                'content': content[:100],
                'latency_ms': round(elapsed),
            }
        else:
            msg_txt = r.text[:200]
            print(f'❌ HTTP {r.status_code}: {msg_txt}')
            result['on'] = {'ok': False, 'error': f'HTTP {r.status_code}: {msg_txt}'}
    except Exception as e:
        print(f'❌ {e}')
        result['on'] = {'ok': False, 'error': str(e)}

    time.sleep(2)

    # 4b. thinking OFF
    print(f'    → OFF ...', end=' ', flush=True)
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'What is 15 * 23? Just the number.'}],
        'max_tokens': 256,
        'stream': False,
    }
    if 'gemini' in model.lower():
        body['extra_body'] = {'google': {'thinking_config': {'thinking_budget': 0}}}
    # For MiniMax, try with temperature=0 / lower max_tokens — no explicit thinking off
    t0 = time.time()
    try:
        r = requests.post(CHAT_URL, headers=h(api_key), json=body, proxies=NP, timeout=60)
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200:
            data = r.json()
            msg = data.get('choices', [{}])[0].get('message', {})
            content = msg.get('content', '')
            usage = data.get('usage', {})
            ctd = usage.get('completion_tokens_details', {})
            reasoning_tokens = ctd.get('reasoning_tokens', 0)
            has_think_tags = '<think>' in content
            print(f'✅ {elapsed:.0f}ms | reasoning_tokens={reasoning_tokens} | '
                  f'has_think_tags={has_think_tags} | content="{content[:60]}"')
            result['off'] = {
                'ok': True,
                'reasoning_tokens': reasoning_tokens,
                'has_think_tags': has_think_tags,
                'content': content[:100],
                'latency_ms': round(elapsed),
            }
        else:
            msg_txt = r.text[:200]
            print(f'❌ HTTP {r.status_code}: {msg_txt}')
            result['off'] = {'ok': False, 'error': f'HTTP {r.status_code}: {msg_txt}'}
    except Exception as e:
        print(f'❌ {e}')
        result['off'] = {'ok': False, 'error': str(e)}

    return result


# ═══════════════════════════════════════════════
#  5) Embedding Models
# ═══════════════════════════════════════════════
def embedding_test(key_name, api_key, model):
    print(f'  📐 Embed: [{key_name}] {model} ...', end=' ', flush=True)
    body = {
        'model': model,
        'input': 'Hello, world! This is a test of the embedding model capabilities.',
    }
    t0 = time.time()
    try:
        r = requests.post(EMBED_URL, headers=h(api_key), json=body, proxies=NP, timeout=30)
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200:
            data = r.json()
            embeds = data.get('data', [])
            if embeds:
                vec = embeds[0].get('embedding', [])
                usage = data.get('usage', {})
                print(f'✅ {elapsed:.0f}ms | dim={len(vec)} | usage={json.dumps(usage)}')
                return {'ok': True, 'dim': len(vec), 'latency_ms': round(elapsed), 'usage': usage}
            else:
                print(f'⚠️ No embeddings in response')
                return {'ok': False, 'error': 'empty data'}
        else:
            msg = r.text[:200]
            print(f'❌ HTTP {r.status_code}: {msg}')
            return {'ok': False, 'error': f'HTTP {r.status_code}: {msg}'}
    except Exception as e:
        print(f'❌ {e}')
        return {'ok': False, 'error': str(e)}


def embedding_rpm_test(key_name, api_key, model, duration=15, concurrency=4):
    """RPM test for embedding models."""
    print(f'  ⏱️  Embed RPM: [{key_name}] {model} ({concurrency}×{duration}s) ...', end=' ', flush=True)
    body = {
        'model': model,
        'input': 'Hello world',
    }
    lock = threading.Lock()
    ok_times, err429, err_other = [], 0, 0
    stop_at = time.time() + duration

    def worker():
        nonlocal err429, err_other
        while time.time() < stop_at:
            t0 = time.time()
            try:
                r = requests.post(EMBED_URL, headers=h(api_key), json=body, proxies=NP, timeout=15)
                lat = (time.time() - t0) * 1000
                if r.status_code == 200:
                    with lock:
                        ok_times.append(lat)
                elif r.status_code == 429:
                    with lock:
                        err429 += 1
                    time.sleep(0.3)
                else:
                    with lock:
                        err_other += 1
            except Exception:
                with lock:
                    err_other += 1

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    t_start = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    wall = time.time() - t_start

    total = len(ok_times) + err429 + err_other
    rpm_raw = len(ok_times) / wall * 60 if wall > 0 else 0

    info = {
        'rpm_raw': round(rpm_raw, 1),
        'success_count': len(ok_times),
        'error_429_count': err429,
        'error_other_count': err_other,
        'total_requests': total,
        'duration_sec': round(wall, 1),
    }
    if ok_times:
        info['avg_latency_ms'] = round(statistics.mean(ok_times), 1)
    print(f'rpm={rpm_raw:.0f} ok={len(ok_times)} 429={err429}')
    return info


# ═══════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════
def main():
    global results
    t_start = time.time()
    results = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'base_url': LLM_BASE_URL,
        'keys': {kn: f'...{kv[-4:]}' for kn, kv in KEYS.items()},
        'chat_models': {},
        'embedding_models': {},
    }

    # ── Chat Models ──
    for model in CHAT_MODELS:
        print(f'\n{"="*60}')
        print(f'  MODEL: {model}')
        print(f'{"="*60}')
        for key_name, api_key in KEYS.items():
            entry_key = f'{key_name}:{model}'
            entry = {'key': key_name, 'model': model}
            print(f'\n  --- {entry_key} ---')

            # Probe
            probe_result = probe(key_name, api_key, model)
            entry['probe'] = probe_result
            if not probe_result.get('alive'):
                results['chat_models'][entry_key] = entry
                continue
            time.sleep(2)

            # Vision
            vision_result = vision_test(key_name, api_key, model)
            entry['vision'] = vision_result
            time.sleep(2)

            # Thinking
            thinking_result = thinking_test(key_name, api_key, model)
            entry['thinking'] = thinking_result
            time.sleep(2)

            # RPM
            rpm_result = rpm_test(key_name, api_key, model)
            entry['rpm'] = rpm_result

            results['chat_models'][entry_key] = entry
            time.sleep(3)

    # ── Embedding Models ──
    for model in EMBED_MODELS:
        print(f'\n{"="*60}')
        print(f'  EMBEDDING: {model}')
        print(f'{"="*60}')
        for key_name, api_key in KEYS.items():
            entry_key = f'{key_name}:{model}'
            entry = {'key': key_name, 'model': model}
            print(f'\n  --- {entry_key} ---')

            # Test basic functionality
            embed_result = embedding_test(key_name, api_key, model)
            entry['probe'] = embed_result
            if not embed_result.get('ok'):
                results['embedding_models'][entry_key] = entry
                continue
            time.sleep(1)

            # RPM test
            rpm_result = embedding_rpm_test(key_name, api_key, model)
            entry['rpm'] = rpm_result

            results['embedding_models'][entry_key] = entry
            time.sleep(2)

    elapsed = time.time() - t_start
    results['elapsed_sec'] = round(elapsed, 1)

    # Save
    out_path = os.path.join(os.path.dirname(__file__), 'benchmark_new_models_results.json')
    with open(out_path, 'w') as f:
        json.dump(results, f, indent=2)
    print(f'\n\n✅ Benchmark complete in {elapsed:.0f}s → {out_path}')

    # Summary
    print('\n' + '='*60)
    print('  SUMMARY')
    print('='*60)
    for ek, ev in results['chat_models'].items():
        alive = ev.get('probe', {}).get('alive', False)
        rpm = ev.get('rpm', {}).get('rpm_effective', 0)
        vis = ev.get('vision', {}).get('vision_ok', '?')
        think_on = ev.get('thinking', {}).get('on', {}).get('ok', '?')
        think_rt = ev.get('thinking', {}).get('on', {}).get('reasoning_tokens', 0)
        print(f'  {ek}: alive={alive} rpm={rpm:.0f} vision={vis} thinking={think_on} rt={think_rt}')
    for ek, ev in results['embedding_models'].items():
        ok = ev.get('probe', {}).get('ok', False)
        dim = ev.get('probe', {}).get('dim', '?')
        rpm = ev.get('rpm', {}).get('rpm_raw', 0)
        print(f'  {ek}: ok={ok} dim={dim} rpm={rpm:.0f}')

if __name__ == '__main__':
    main()
