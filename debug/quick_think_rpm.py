#!/usr/bin/env python3
"""Quick thinking + RPM tests for gemini-3.1-pro-preview and MiniMax-M2.7."""
import json, os, sys, time, logging, requests, re
from threading import Thread, Event, Lock

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from lib import LLM_API_KEYS, LLM_BASE_URL

CHAT_URL = f'{LLM_BASE_URL}/chat/completions'
KEYS = {f'key_{i}': k for i, k in enumerate(LLM_API_KEYS)}

def h(api_key):
    return {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}
NP = {'http': None, 'https': None}


def test_thinking_streaming(key_name, api_key, model):
    """Test streaming with thinking detection."""
    print(f'\n🧠 Streaming thinking test: [{key_name}] {model}')
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'What is 17*23? Be brief.'}],
        'max_tokens': 2048,
        'temperature': 1.0,
        'stream': True,
    }
    t0 = time.time()
    thinking = ''
    content = ''
    has_reasoning_field = False

    try:
        r = requests.post(CHAT_URL, headers=h(api_key), json=body,
                          timeout=120, stream=True, proxies=NP)
        if r.status_code != 200:
            print(f'  ❌ HTTP {r.status_code}: {r.text[:200]}')
            return

        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith('data: '): continue
            payload = line[6:].strip()
            if payload == '[DONE]': break
            try:
                chunk = json.loads(payload)
                delta = chunk.get('choices', [{}])[0].get('delta', {})
                rc = delta.get('reasoning_content') or delta.get('thinking', '')
                if rc:
                    thinking += rc
                    has_reasoning_field = True
                cd = delta.get('content', '')
                if cd:
                    content += cd
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                logger.debug('Skipping unparseable SSE chunk: %s', exc)
        r.close()
        lat = (time.time() - t0) * 1000

        # Check <think> tags
        has_think_tags = '<think>' in content
        if has_think_tags:
            m = re.search(r'<think>(.*?)</think>', content, re.DOTALL)
            if m:
                think_tag_content = m.group(1)
                clean_content = re.sub(r'<think>.*?</think>\s*', '', content, flags=re.DOTALL)
                print(f'  ✅ <think> tags found')
                print(f'     Think: {think_tag_content[:150]}...')
                print(f'     Content: {clean_content[:150]}')
            else:
                print(f'  ⚠️ Unclosed <think> tag')
                print(f'     Raw content: {content[:300]}')
        elif has_reasoning_field:
            print(f'  ✅ reasoning_content field')
            print(f'     Think ({len(thinking)}c): {thinking[:150]}...')
            print(f'     Content ({len(content)}c): {content[:150]}')
        else:
            print(f'  ⚠️ No thinking detected')
            print(f'     Content ({len(content)}c): {content[:200]}')

        print(f'     Latency: {lat:.0f}ms')
    except Exception as e:
        print(f'  ❌ {e}')


def test_vision(key_name, api_key, model):
    """Test vision capability."""
    print(f'\n👁️ Vision test: [{key_name}] {model}')
    import base64, struct, zlib
    width, height = 2, 2
    raw = b'\x00\xff\x00\x00\xff\x00\xff\x00\xff\x00\x00\x00\xff\xff\xff\xff\xff\xff'
    def chunk(ct, d):
        c = ct + d
        return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    png = sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
    b64 = base64.b64encode(png).decode()

    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'What colors are in this image? Brief answer.'},
            {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
        ]}],
        'max_tokens': 200, 'temperature': 0,
    }
    t0 = time.time()
    try:
        r = requests.post(CHAT_URL, headers=h(api_key), json=body, timeout=90, proxies=NP)
        lat = (time.time() - t0) * 1000
        if r.status_code == 200:
            data = r.json()
            c = data.get('choices',[{}])[0].get('message',{}).get('content','')
            # For M2.7, strip <think> tags
            if '<think>' in (c or ''):
                c = re.sub(r'<think>.*?</think>\s*', '', c, flags=re.DOTALL)
            print(f'  ✅ {lat:.0f}ms: "{(c or "")[:150]}"')
        else:
            print(f'  ❌ HTTP {r.status_code}: {r.text[:200]}')
    except Exception as e:
        print(f'  ❌ {e}')


def test_rpm(key_name, api_key, model, concurrency=4, duration_sec=20):
    """Quick RPM test."""
    print(f'\n🔄 RPM: [{key_name}] {model} (c={concurrency}, {duration_sec}s)')
    stop = Event()
    lock = Lock()
    c = {'ok': 0, '429': 0, 'err': 0}

    def worker():
        while not stop.is_set():
            body = {'model': model, 'messages': [{'role': 'user', 'content': 'Hi'}],
                    'max_tokens': 10, 'temperature': 0}
            try:
                r = requests.post(CHAT_URL, headers=h(api_key), json=body, timeout=30, proxies=NP)
                with lock:
                    if r.status_code == 200: c['ok'] += 1
                    elif r.status_code == 429: c['429'] += 1; time.sleep(1.0)
                    else: c['err'] += 1
            except Exception as exc:
                logger.debug('Request error: %s', exc)
                with lock: c['err'] += 1
            time.sleep(0.05)

    threads = [Thread(target=worker, daemon=True) for _ in range(concurrency)]
    t0 = time.time()
    for t in threads: t.start()
    time.sleep(duration_sec)
    stop.set()
    for t in threads: t.join(timeout=5)
    elapsed = time.time() - t0
    rpm = (c['ok'] / elapsed) * 60 if elapsed > 0 else 0
    print(f'  → RPM≈{rpm:.0f}  ok={c["ok"]} 429s={c["429"]} err={c["err"]}')
    return rpm


# Run all tests
results = {}

for model in ['gemini-3.1-pro-preview', 'MiniMax-M2.7']:
    results[model] = {}
    for kn, ak in KEYS.items():
        # Thinking
        test_thinking_streaming(kn, ak, model)
        # Vision
        test_vision(kn, ak, model)
        # RPM
        rpm = test_rpm(kn, ak, model)
        results[model][kn] = {'rpm': rpm}

# Also RPM for image model on key_1 (key_0 was rate limited)
print('\n=== gemini-3.1-flash-image-preview RPM ===')
for kn, ak in KEYS.items():
    rpm = test_rpm(kn, ak, 'gemini-3.1-flash-image-preview', concurrency=2, duration_sec=15)
    results.setdefault('gemini-3.1-flash-image-preview', {})[kn] = {'rpm': rpm}

print(f'\n{"="*60}')
print('Summary:')
for model, keys_data in results.items():
    for kn, data in keys_data.items():
        print(f'  {model} [{kn}]: RPM≈{data["rpm"]:.0f}')
print(f'{"="*60}')
