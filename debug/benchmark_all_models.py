#!/usr/bin/env python3
"""benchmark_all_models.py — Test all configured API keys × models for RPM & speed.

Auto-probes each key against every model, then measures:
  • Probe   — Can this key call this model? (filters unavailable combos)
  • RPM     — 60s sustained concurrency → actual RPM ceiling
  • Speed   — Streaming: TTFT (time to first token) + output tokens/s
  • Latency — Non-streaming end-to-end latency
  • Vision  — For VLM models: send image prompt, test image understanding

Results → debug/benchmark_results.json, used by lib/llm_dispatch.py for routing.

Usage:
    python debug/benchmark_all_models.py                      # full test
    python debug/benchmark_all_models.py --quick              # quick mode (1 round each)
    python debug/benchmark_all_models.py --models gemini      # only models matching keyword
    python debug/benchmark_all_models.py --rpm-only           # RPM only
    python debug/benchmark_all_models.py --speed-only         # Speed only
    python debug/benchmark_all_models.py --probe-only         # Probe only
"""

import argparse, json, os, sys, time, statistics, base64
from datetime import datetime
from threading import Thread, Event, Lock
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ── Project imports ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from lib import LLM_API_KEYS, LLM_BASE_URL

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), 'benchmark_results.json')
CHAT_URL = f'{LLM_BASE_URL}/chat/completions'

KEYS = {f'key_{i}': k for i, k in enumerate(LLM_API_KEYS)}

# ═══════════════════════════════════════════════════════════
#  Model Registry — all models to test
# ═══════════════════════════════════════════════════════════

ALL_MODELS = [
    # Claude family — use the actual API gateway model names
    {'model': 'aws.claude-opus-4.6',         'tags': ['text', 'vision', 'thinking']},
    {'model': 'aws.claude-opus-4.6-b',       'tags': ['text', 'vision', 'thinking']},
    {'model': 'vertex.claude-opus-4.6',      'tags': ['text', 'vision', 'thinking']},
    {'model': 'aws.claude-sonnet-4.6',       'tags': ['text', 'vision', 'thinking']},
    # Gemini family
    {'model': 'gemini-2.5-pro',              'tags': ['text', 'vision', 'thinking']},
    {'model': 'gemini-3.1-flash-lite-preview', 'tags': ['text', 'vision', 'cheap']},
    # Qwen
    {'model': 'qwen3.6-plus',               'tags': ['text', 'vision', 'thinking']},
    # MiniMax
    {'model': 'MiniMax-M2.5',               'tags': ['text']},
    # Doubao
    {'model': 'Doubao-Seed-2.0-pro',        'tags': ['text', 'thinking']},
]

# ═══════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════

def _headers(api_key):
    return {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }

def _no_proxy():
    """Skip proxy for internal endpoints."""
    return {'http': None, 'https': None}

def _simple_body(model, prompt='Say "hello" in one word.', max_tokens=50, stream=False):
    """Build a minimal request body, respecting model-specific constraints."""
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
        'stream': stream,
        'temperature': 0,
    }
    return body

def _make_test_png_b64():
    """Create a 2x2 multi-color PNG for vision testing (more reliable than 1x1)."""
    import struct, zlib
    width, height = 2, 2
    # RGBA: red, green, blue, white
    raw = b''
    raw += b'\x00' + b'\xff\x00\x00\xff' + b'\x00\xff\x00\xff'  # row 0
    raw += b'\x00' + b'\x00\x00\xff\xff' + b'\xff\xff\xff\xff'  # row 1
    def chunk(ctype, data):
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    png = sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
    import base64
    return base64.b64encode(png).decode()

def _vision_body(model, max_tokens=100):
    """Build a vision test body with a tiny 2x2 multi-color PNG."""
    tiny_png_b64 = _make_test_png_b64()
    temp = 0
    return {
        'model': model,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'What color is this image? Answer in one word.'},
                {'type': 'image_url', 'image_url': {
                    'url': f'data:image/png;base64,{tiny_png_b64}'
                }},
            ]
        }],
        'max_tokens': max_tokens,
        'temperature': temp,
        'stream': False,
    }


# ═══════════════════════════════════════════════════════════
#  Phase 1: Probe — check if (key, model) is alive
# ═══════════════════════════════════════════════════════════

def probe_model(key_name, api_key, model, timeout=30):
    """Send a minimal request to check if this (key, model) works.
    Returns dict with alive, probe_latency_ms, error.
    """
    print(f'  🔍 Probe: [{key_name}] {model} ...', end=' ', flush=True)
    body = _simple_body(model, prompt='Hi', max_tokens=10)
    t0 = time.time()
    try:
        resp = requests.post(
            CHAT_URL, headers=_headers(api_key),
            json=body, timeout=timeout, proxies=_no_proxy(),
        )
        latency = (time.time() - t0) * 1000
        if resp.status_code == 200:
            data = resp.json()
            content = ''
            choices = data.get('choices', [])
            if choices and choices[0] is not None:
                msg = choices[0].get('message') or {}
                content = (msg.get('content', '') or '')[:50]
            print(f'✅ {latency:.0f}ms "{content}"')
            return {'alive': True, 'probe_latency_ms': round(latency, 1), 'preview': content}
        elif resp.status_code == 429:
            # 429 = rate limited but model IS available on this key
            err_text = resp.text[:200]
            print(f'⚠️ 429 rate-limited (model available but busy): {err_text}')
            return {'alive': True, 'probe_latency_ms': round(latency, 1),
                    'rate_limited': True, 'detail': err_text}
        else:
            err_text = resp.text[:200]
            print(f'❌ HTTP {resp.status_code}: {err_text}')
            return {'alive': False, 'probe_latency_ms': round(latency, 1),
                    'error': f'HTTP {resp.status_code}', 'detail': err_text}
    except Exception as e:
        latency = (time.time() - t0) * 1000
        print(f'❌ {e}')
        return {'alive': False, 'probe_latency_ms': round(latency, 1), 'error': str(e)}


# ═══════════════════════════════════════════════════════════
#  Phase 2: RPM — measure max requests per minute
# ═══════════════════════════════════════════════════════════

def test_rpm(key_name, api_key, model, concurrency=8, duration_sec=30):
    """Measure RPM by sending concurrent requests for `duration_sec` seconds.
    Returns dict with rpm_raw, rpm_effective, success_count, error_429_count, etc.
    """
    print(f'\n  🔄 RPM test: [{key_name}] {model} (concurrency={concurrency}, {duration_sec}s)')

    stop_event = Event()
    counter_lock = Lock()
    counters = {'success': 0, 'error_429': 0, 'error_other': 0}
    latencies = []

    def _worker():
        while not stop_event.is_set():
            body = _simple_body(model, prompt='Count to 3.', max_tokens=20)
            t0 = time.time()
            try:
                resp = requests.post(
                    CHAT_URL, headers=_headers(api_key),
                    json=body, timeout=30, proxies=_no_proxy(),
                )
                lat = (time.time() - t0) * 1000
                with counter_lock:
                    if resp.status_code == 200:
                        counters['success'] += 1
                        latencies.append(lat)
                    elif resp.status_code == 429:
                        counters['error_429'] += 1
                        # Back off briefly on 429 to avoid wasting time
                        time.sleep(1.0)
                    else:
                        counters['error_other'] += 1
            except Exception:
                with counter_lock:
                    counters['error_other'] += 1
            # Small sleep to avoid busy-loop
            time.sleep(0.05)

    threads = [Thread(target=_worker, daemon=True) for _ in range(concurrency)]
    t_start = time.time()
    for t in threads:
        t.start()

    # Wait for duration
    time.sleep(duration_sec)
    stop_event.set()
    for t in threads:
        t.join(timeout=5)

    elapsed = time.time() - t_start
    total = counters['success'] + counters['error_429'] + counters['error_other']
    rpm_raw = (counters['success'] / elapsed) * 60 if elapsed > 0 else 0
    # Effective RPM = successful requests scaled to 1 minute
    rpm_effective = rpm_raw

    result = {
        'rpm_raw': round(rpm_raw, 1),
        'rpm_effective': round(rpm_effective, 1),
        'success_count': counters['success'],
        'error_429_count': counters['error_429'],
        'error_other_count': counters['error_other'],
        'total_requests': total,
        'duration_sec': round(elapsed, 1),
        'concurrency': concurrency,
    }

    if latencies:
        result['avg_latency_ms'] = round(statistics.mean(latencies), 1)
        result['p50_latency_ms'] = round(statistics.median(latencies), 1)
        if len(latencies) >= 10:
            sorted_lat = sorted(latencies)
            result['p95_latency_ms'] = round(sorted_lat[int(len(sorted_lat) * 0.95)], 1)

    # If many 429s, the effective RPM is roughly proportional to success ratio
    if counters['error_429'] > 0 and total > 0:
        ratio_ok = counters['success'] / total
        result['_429_ratio'] = round(1 - ratio_ok, 3)

    print(f'     → RPM≈{rpm_effective:.0f}  success={counters["success"]} '
          f'429s={counters["error_429"]} other_err={counters["error_other"]} '
          f'elapsed={elapsed:.1f}s')
    return result


# ═══════════════════════════════════════════════════════════
#  Phase 3: Speed — streaming TTFT + tokens/sec
# ═══════════════════════════════════════════════════════════

def test_speed(key_name, api_key, model, runs=3):
    """Measure streaming speed: TTFT (time to first token) and tokens/sec.
    Returns dict with avg_ttft_ms, avg_tokens_per_sec, etc.
    """
    print(f'\n  ⚡ Speed test: [{key_name}] {model} ({runs} runs)')

    prompt = (
        'Write a short 100-word essay about artificial intelligence. '
        'Be concise and informative.'
    )
    all_ttft = []
    all_tps = []
    all_total_tokens = []

    for run_i in range(runs):
        body = _simple_body(model, prompt=prompt, max_tokens=256, stream=True)
        t0 = time.time()
        ttft = None
        token_count = 0

        try:
            resp = requests.post(
                CHAT_URL, headers=_headers(api_key),
                json=body, timeout=60, stream=True, proxies=_no_proxy(),
            )
            if resp.status_code != 200:
                print(f'    Run {run_i+1}: HTTP {resp.status_code}')
                continue

            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith('data: '):
                    continue
                payload = line[6:].strip()
                if payload == '[DONE]':
                    break
                try:
                    chunk = json.loads(payload)
                    delta = chunk.get('choices', [{}])[0].get('delta', {})
                    content = delta.get('content', '')
                    if content:
                        if ttft is None:
                            ttft = (time.time() - t0) * 1000
                        token_count += len(content.split())  # rough word-level count
                except (json.JSONDecodeError, IndexError, KeyError):
                    pass

            resp.close()
            total_time = (time.time() - t0) * 1000

            if ttft is not None:
                all_ttft.append(ttft)
                # More accurate: use usage from last chunk if available
                # Rough token estimate: ~1.3 tokens per word
                est_tokens = max(token_count * 1.3, 1)
                generation_time_sec = max((total_time - ttft) / 1000, 0.01)
                tps = est_tokens / generation_time_sec
                all_tps.append(tps)
                all_total_tokens.append(est_tokens)
                print(f'    Run {run_i+1}: TTFT={ttft:.0f}ms  tokens≈{est_tokens:.0f}  '
                      f'TPS≈{tps:.1f}  total={total_time:.0f}ms')
            else:
                print(f'    Run {run_i+1}: no content tokens received')

        except Exception as e:
            print(f'    Run {run_i+1}: ERROR {e}')
            continue

    if not all_ttft:
        return {'error': 'all speed runs failed'}

    return {
        'avg_ttft_ms': round(statistics.mean(all_ttft), 1),
        'min_ttft_ms': round(min(all_ttft), 1),
        'max_ttft_ms': round(max(all_ttft), 1),
        'avg_tokens_per_sec': round(statistics.mean(all_tps), 1),
        'min_tokens_per_sec': round(min(all_tps), 1),
        'max_tokens_per_sec': round(max(all_tps), 1),
        'avg_total_tokens': round(statistics.mean(all_total_tokens), 1),
        'successful_runs': len(all_ttft),
    }


# ═══════════════════════════════════════════════════════════
#  Phase 4: Latency — non-streaming end-to-end
# ═══════════════════════════════════════════════════════════

def test_latency(key_name, api_key, model, runs=3):
    """Measure non-streaming E2E latency.
    Returns dict with avg_latency_ms, min, max, p50, etc.
    """
    print(f'\n  🕐 Latency test: [{key_name}] {model} ({runs} runs)')
    latencies = []

    for run_i in range(runs):
        body = _simple_body(model, prompt='What is 2+2? Answer with just the number.', max_tokens=10)
        t0 = time.time()
        try:
            resp = requests.post(
                CHAT_URL, headers=_headers(api_key),
                json=body, timeout=30, proxies=_no_proxy(),
            )
            lat = (time.time() - t0) * 1000
            if resp.status_code == 200:
                latencies.append(lat)
                print(f'    Run {run_i+1}: {lat:.0f}ms')
            else:
                print(f'    Run {run_i+1}: HTTP {resp.status_code}')
        except Exception as e:
            print(f'    Run {run_i+1}: ERROR {e}')

    if not latencies:
        return {'error': 'all latency runs failed'}

    result = {
        'avg_latency_ms': round(statistics.mean(latencies), 1),
        'min_latency_ms': round(min(latencies), 1),
        'max_latency_ms': round(max(latencies), 1),
        'p50_latency_ms': round(statistics.median(latencies), 1),
        'successful_runs': len(latencies),
    }
    if len(latencies) >= 5:
        sorted_lat = sorted(latencies)
        result['p95_latency_ms'] = round(sorted_lat[int(len(sorted_lat) * 0.95)], 1)
    return result


# ═══════════════════════════════════════════════════════════
#  Phase 5: Vision — test image understanding
# ═══════════════════════════════════════════════════════════

def test_vision(key_name, api_key, model, timeout=30):
    """Test vision capability with a tiny image.
    Returns dict with vision_ok, latency_ms, response.
    """
    print(f'  👁️  Vision test: [{key_name}] {model} ...', end=' ', flush=True)
    body = _vision_body(model)
    t0 = time.time()
    try:
        resp = requests.post(
            CHAT_URL, headers=_headers(api_key),
            json=body, timeout=timeout, proxies=_no_proxy(),
        )
        lat = (time.time() - t0) * 1000
        if resp.status_code == 200:
            data = resp.json()
            content = ''
            choices = data.get('choices', [])
            if choices:
                content = (choices[0].get('message', {}).get('content', '') or '')[:100]
            ok = bool(content.strip())
            print(f'{"✅" if ok else "⚠️"} {lat:.0f}ms "{content[:50]}"')
            return {'vision_ok': ok, 'latency_ms': round(lat, 1), 'response': content[:100]}
        else:
            err_text = resp.text[:200]
            print(f'❌ HTTP {resp.status_code}')
            return {'vision_ok': False, 'latency_ms': round(lat, 1),
                    'error': f'HTTP {resp.status_code}', 'detail': err_text}
    except Exception as e:
        lat = (time.time() - t0) * 1000
        print(f'❌ {e}')
        return {'vision_ok': False, 'latency_ms': round(lat, 1), 'error': str(e)}


# ═══════════════════════════════════════════════════════════
#  Orchestrator — run all phases
# ═══════════════════════════════════════════════════════════

def run_benchmark(args):
    """Run the full benchmark pipeline."""
    data = {
        'timestamp': datetime.now().isoformat(),
        'base_url': LLM_BASE_URL,
        'keys': {name: f'...{key[-4:]}' for name, key in KEYS.items() if key},
        'models': {},
    }

    # Filter models by keyword if specified
    models_to_test = ALL_MODELS
    if args.models:
        keywords = [k.lower() for k in args.models]
        models_to_test = [m for m in ALL_MODELS
                          if any(k in m['model'].lower() for k in keywords)]
        print(f'🔎 Filtered to {len(models_to_test)} models: '
              f'{[m["model"] for m in models_to_test]}')

    active_keys = {k: v for k, v in KEYS.items() if v}
    total_pairs = len(models_to_test) * len(active_keys)
    print(f'\n{"="*70}')
    print(f'  Benchmark: {len(active_keys)} keys × {len(models_to_test)} models = {total_pairs} pairs')
    print(f'  Mode: {"quick" if args.quick else "full"} | '
          f'Phases: {"ALL" if not any([args.probe_only, args.rpm_only, args.speed_only]) else "selected"}')
    print(f'{"="*70}\n')

    # Phase selection
    do_all = not any([args.probe_only, args.rpm_only, args.speed_only])
    do_probe = do_all or args.probe_only
    do_rpm = do_all or args.rpm_only
    do_speed = do_all or args.speed_only
    do_latency = do_all
    do_vision = do_all

    # ── Phase 1: Probe all (key, model) pairs in parallel ──
    if do_probe:
        print(f'\n{"─"*60}')
        print(f'  Phase 1/5: PROBE — checking which (key, model) pairs are alive')
        print(f'{"─"*60}')

        alive_pairs = {}  # key → set of alive model names

        with ThreadPoolExecutor(max_workers=6) as pool:
            probe_futures = {}
            for key_name, api_key in active_keys.items():
                for minfo in models_to_test:
                    model = minfo['model']
                    entry_key = f'{key_name}:{model}'
                    fut = pool.submit(probe_model, key_name, api_key, model)
                    probe_futures[fut] = (key_name, model, entry_key, minfo['tags'])

            for fut in as_completed(probe_futures):
                key_name, model, entry_key, tags = probe_futures[fut]
                result = fut.result()

                if entry_key not in data['models']:
                    data['models'][entry_key] = {
                        'key': key_name, 'model': model, 'tags': tags,
                    }
                data['models'][entry_key]['probe'] = result

                if result.get('alive'):
                    if key_name not in alive_pairs:
                        alive_pairs[key_name] = set()
                    alive_pairs[key_name].add(model)

        # Summary
        for key_name in active_keys:
            models_alive = alive_pairs.get(key_name, set())
            models_dead = {m['model'] for m in models_to_test} - models_alive
            print(f'\n  [{key_name}] alive={len(models_alive)} dead={len(models_dead)}')
            if models_dead:
                print(f'    Dead: {sorted(models_dead)}')
    else:
        # If skipping probe, assume all alive
        alive_pairs = {}
        for key_name in active_keys:
            alive_pairs[key_name] = {m['model'] for m in models_to_test}
        for key_name, api_key in active_keys.items():
            for minfo in models_to_test:
                entry_key = f'{key_name}:{minfo["model"]}'
                if entry_key not in data['models']:
                    data['models'][entry_key] = {
                        'key': key_name, 'model': minfo['model'], 'tags': minfo['tags'],
                        'probe': {'alive': True},
                    }

    # ── Phase 2: RPM ──
    if do_rpm:
        print(f'\n{"─"*60}')
        print(f'  Phase 2/5: RPM — measuring max requests per minute')
        print(f'{"─"*60}')

        rpm_concurrency = 4 if args.quick else 8
        rpm_duration = 15 if args.quick else 30

        for key_name, api_key in active_keys.items():
            for minfo in models_to_test:
                model = minfo['model']
                if model not in alive_pairs.get(key_name, set()):
                    continue
                entry_key = f'{key_name}:{model}'
                result = test_rpm(key_name, api_key, model,
                                  concurrency=rpm_concurrency,
                                  duration_sec=rpm_duration)
                data['models'][entry_key]['rpm'] = result

    # ── Phase 3: Speed ──
    if do_speed:
        print(f'\n{"─"*60}')
        print(f'  Phase 3/5: SPEED — streaming TTFT + tokens/sec')
        print(f'{"─"*60}')

        speed_runs = 1 if args.quick else 3

        for key_name, api_key in active_keys.items():
            for minfo in models_to_test:
                model = minfo['model']
                if model not in alive_pairs.get(key_name, set()):
                    continue
                entry_key = f'{key_name}:{model}'
                result = test_speed(key_name, api_key, model, runs=speed_runs)
                data['models'][entry_key]['speed'] = result

    # ── Phase 4: Latency ──
    if do_latency:
        print(f'\n{"─"*60}')
        print(f'  Phase 4/5: LATENCY — non-streaming end-to-end')
        print(f'{"─"*60}')

        latency_runs = 1 if args.quick else 3

        for key_name, api_key in active_keys.items():
            for minfo in models_to_test:
                model = minfo['model']
                if model not in alive_pairs.get(key_name, set()):
                    continue
                entry_key = f'{key_name}:{model}'
                result = test_latency(key_name, api_key, model, runs=latency_runs)
                data['models'][entry_key]['latency'] = result

    # ── Phase 5: Vision ──
    if do_vision:
        print(f'\n{"─"*60}')
        print(f'  Phase 5/5: VISION — testing image understanding')
        print(f'{"─"*60}')

        vision_models = [m for m in models_to_test if 'vision' in m['tags']]
        if vision_models:
            for key_name, api_key in active_keys.items():
                for minfo in vision_models:
                    model = minfo['model']
                    if model not in alive_pairs.get(key_name, set()):
                        continue
                    entry_key = f'{key_name}:{model}'
                    result = test_vision(key_name, api_key, model)
                    data['models'][entry_key]['vision'] = result
        else:
            print('  No vision-capable models in test set.')

    return data


# ═══════════════════════════════════════════════════════════
#  Summary Table
# ═══════════════════════════════════════════════════════════

def print_summary_table(data):
    """Print a nice summary table."""
    models = data.get('models', {})
    if not models:
        print('No results.')
        return

    print(f'\n{"="*110}')
    print(f'  {"Key":<10} {"Model":<38} {"Alive":>5} {"RPM":>6} {"TTFT":>8} '
          f'{"TPS":>6} {"Latency":>8} {"Vision":>6}')
    print(f'{"─"*110}')

    for entry_key in sorted(models.keys()):
        entry = models[entry_key]
        key = entry.get('key', '?')
        model = entry.get('model', '?')

        probe = entry.get('probe', {})
        alive = '✅' if probe.get('alive') else '❌'

        rpm_data = entry.get('rpm', {})
        rpm_str = f'{rpm_data["rpm_effective"]:.0f}' if 'rpm_effective' in rpm_data else '—'

        speed = entry.get('speed', {})
        ttft_str = f'{speed["avg_ttft_ms"]:.0f}ms' if 'avg_ttft_ms' in speed else '—'
        tps_str = f'{speed["avg_tokens_per_sec"]:.0f}' if 'avg_tokens_per_sec' in speed else '—'

        lat_data = entry.get('latency', {})
        lat_str = f'{lat_data["avg_latency_ms"]:.0f}ms' if 'avg_latency_ms' in lat_data else '—'

        vis_data = entry.get('vision', {})
        vis_str = '✅' if vis_data.get('vision_ok') else ('❌' if 'vision_ok' in vis_data else '—')

        print(f'  {key:<10} {model:<38} {alive:>5} {rpm_str:>6} {ttft_str:>8} '
              f'{tps_str:>6} {lat_str:>8} {vis_str:>6}')

    print(f'{"="*110}')

    # Dispatch recommendation
    print(f'\n📊 Dispatch Hints:')

    # Group by model, find which keys support it
    model_keys = {}
    for entry_key, entry in models.items():
        model = entry.get('model', '')
        key = entry.get('key', '')
        alive = entry.get('probe', {}).get('alive', False)
        if alive:
            if model not in model_keys:
                model_keys[model] = []
            model_keys[model].append(key)

    for model_name, keys in sorted(model_keys.items()):
        keys_str = ', '.join(sorted(keys))
        best_rpm = 0
        best_lat = float('inf')
        for k in keys:
            ek = f'{k}:{model_name}'
            rpm_data = models.get(ek, {}).get('rpm', {})
            best_rpm = max(best_rpm, rpm_data.get('rpm_effective', 0))
            lat_data = models.get(ek, {}).get('latency', {})
            if lat_data.get('avg_latency_ms', float('inf')) < best_lat:
                best_lat = lat_data.get('avg_latency_ms', float('inf'))

        lat_str = f'{best_lat:.0f}ms' if best_lat < float('inf') else '—'
        print(f'   {model_name:<40} keys=[{keys_str}]  '
              f'best_rpm={best_rpm:.0f}  best_lat={lat_str}')


# ═══════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Benchmark all LLM models across API keys')
    parser.add_argument('--quick', action='store_true',
                        help='Quick mode: fewer runs per test')
    parser.add_argument('--models', nargs='+',
                        help='Only test models containing these keywords')
    parser.add_argument('--probe-only', action='store_true',
                        help='Only run probe phase')
    parser.add_argument('--rpm-only', action='store_true',
                        help='Only run RPM phase')
    parser.add_argument('--speed-only', action='store_true',
                        help='Only run speed phase')
    parser.add_argument('--output', default=OUTPUT_FILE,
                        help=f'Output JSON file (default: {OUTPUT_FILE})')
    args = parser.parse_args()

    print(f'🚀 LLM Benchmark — {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'   Base URL: {LLM_BASE_URL}')
    print(f'   Keys: {", ".join(f"{k}=...{v[-4:]}" for k, v in KEYS.items() if v)}')
    print(f'   Models: {len(ALL_MODELS)}')

    t_start = time.time()
    data = run_benchmark(args)
    elapsed = time.time() - t_start

    data['elapsed_sec'] = round(elapsed, 1)

    # Save results
    output_file = args.output
    os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
    with open(output_file, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f'\n💾 Results saved to {output_file}')

    print_summary_table(data)
    print(f'\n⏱️  Total benchmark time: {elapsed:.1f}s')
    print(f'✅ Benchmark complete!')


if __name__ == '__main__':
    main()
