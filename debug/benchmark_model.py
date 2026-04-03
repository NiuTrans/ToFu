"""benchmark_model.py — Test a model's generation speed (tokens/s) and RPM ceiling.

Usage:
    python debug/benchmark_model.py                          # default model
    python debug/benchmark_model.py --model qwen3.5-plus     # test other model
    python debug/benchmark_model.py --rpm-only               # RPM only
    python debug/benchmark_model.py --speed-only             # speed only
"""

import sys, os, json, time, argparse, logging, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

logger = logging.getLogger(__name__)

# ── 把项目根目录加入 path ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from lib import LLM_API_KEY, LLM_BASE_URL

# ═══════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════
DEFAULT_MODEL = 'gemini-2.5-pro'

HEADERS = {
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {LLM_API_KEY}',
}
CHAT_URL = f'{LLM_BASE_URL}/chat/completions'

# 测试 prompt（简短，让模型生成较长回复以测速度）
SPEED_PROMPT = "请详细解释什么是 Transformer 架构，包括 self-attention 的计算过程、多头注意力的原理、位置编码的作用，并举一个简单的数值例子。"
RPM_PROMPT = "Say 'hello world' in 5 different programming languages."

# ═══════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════

def make_body(model, prompt, stream=False, max_tokens=2048, temperature=0.7):
    return {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
        'temperature': temperature,
        'stream': stream,
    }


# ═══════════════════════════════════════════════════════
#  测试 1: 生成速度 (Streaming)
# ═══════════════════════════════════════════════════════

def test_speed_streaming(model, max_tokens=2048, runs=3):
    """通过 streaming 测量 TTFT 和 output tokens/s"""
    print(f'\n{"="*60}')
    print(f'  🚀 生成速度测试 (Streaming) — {model}')
    print(f'  max_tokens={max_tokens}, runs={runs}')
    print(f'{"="*60}')

    all_results = []

    for run_i in range(runs):
        print(f'\n--- Run {run_i+1}/{runs} ---')
        body = make_body(model, SPEED_PROMPT, stream=True, max_tokens=max_tokens)

        # 重试逻辑 for 429
        resp = None
        for retry in range(8):
            t_start = time.time()
            try:
                resp = requests.post(CHAT_URL, headers=HEADERS, json=body,
                                     stream=True, timeout=(30, 300))
                if resp.status_code == 429:
                    wait = min(2 ** (retry + 1), 30)
                    print(f'  ⚠️  429 限流, {wait}s 后重试 ({retry+1}/8)...')
                    resp.close()
                    resp = None
                    time.sleep(wait)
                    continue
                elif resp.status_code != 200:
                    print(f'  ❌ HTTP {resp.status_code}: {resp.text[:300]}')
                    resp.close()
                    resp = None
                    break
                else:
                    break
            except Exception as e:
                print(f'  ❌ 连接错误: {e}')
                resp = None
                break

        if resp is None or resp.status_code != 200:
            print(f'  跳过本轮')
            continue

        ttft = None
        content = ''
        chunk_count = 0
        output_tokens = 0
        usage = None

        try:

            resp.encoding = 'utf-8'
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith('data: '):
                    continue
                data_str = line[6:].strip()
                if data_str == '[DONE]':
                    break
                try:
                    chunk = json.loads(data_str)
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.debug('Skipping unparseable SSE chunk: %s', exc)
                    continue

                if chunk.get('usage'):
                    usage = chunk['usage']

                choices = chunk.get('choices', [])
                if not choices:
                    continue

                delta = choices[0].get('delta', {})
                if choices[0].get('usage'):
                    usage = choices[0]['usage']

                cd = delta.get('content', '')
                if cd:
                    if ttft is None:
                        ttft = time.time() - t_start
                    content += cd
                    chunk_count += 1

            resp.close()
        except Exception as e:
            print(f'  ❌ Error: {e}')
            continue

        t_total = time.time() - t_start

        # 从 usage 获取准确 token 数，否则估算
        if usage:
            output_tokens = usage.get('completion_tokens', 0)
            input_tokens = usage.get('prompt_tokens', 0)
            total_tokens = usage.get('total_tokens', 0)
        else:
            # 粗略估算：中文约 1.5 token/字，英文约 1.3 token/word
            output_tokens = max(len(content) // 2, chunk_count)
            input_tokens = 0
            total_tokens = output_tokens

        # 生成阶段时间 = 总时间 - TTFT
        gen_time = t_total - (ttft or 0)
        speed = output_tokens / gen_time if gen_time > 0 else 0

        result = {
            'ttft': ttft,
            'total_time': t_total,
            'gen_time': gen_time,
            'output_tokens': output_tokens,
            'input_tokens': input_tokens,
            'speed_tps': speed,
            'content_len': len(content),
        }
        all_results.append(result)

        print(f'  TTFT:           {ttft:.3f}s' if ttft else '  TTFT:           N/A')
        print(f'  总耗时:         {t_total:.2f}s')
        print(f'  生成耗时:       {gen_time:.2f}s')
        print(f'  输出 tokens:    {output_tokens}')
        print(f'  输入 tokens:    {input_tokens}')
        print(f'  生成速度:       {speed:.1f} tokens/s')
        print(f'  内容长度:       {len(content)} chars')

    if all_results:
        print(f'\n{"─"*60}')
        print(f'  📊 汇总 ({len(all_results)} runs)')
        print(f'{"─"*60}')
        avg_ttft = sum(r['ttft'] for r in all_results if r['ttft']) / len([r for r in all_results if r['ttft']]) if any(r['ttft'] for r in all_results) else 0
        avg_speed = sum(r['speed_tps'] for r in all_results) / len(all_results)
        avg_out = sum(r['output_tokens'] for r in all_results) / len(all_results)
        max_speed = max(r['speed_tps'] for r in all_results)
        min_speed = min(r['speed_tps'] for r in all_results)
        print(f'  平均 TTFT:      {avg_ttft:.3f}s')
        print(f'  平均生成速度:    {avg_speed:.1f} tokens/s')
        print(f'  最快:           {max_speed:.1f} tokens/s')
        print(f'  最慢:           {min_speed:.1f} tokens/s')
        print(f'  平均输出 tokens: {avg_out:.0f}')

    return all_results


# ═══════════════════════════════════════════════════════
#  测试 2: 非流式速度测试
# ═══════════════════════════════════════════════════════

def test_speed_non_streaming(model, max_tokens=2048, runs=3):
    """非流式请求测速"""
    print(f'\n{"="*60}')
    print(f'  🚀 生成速度测试 (Non-Streaming) — {model}')
    print(f'{"="*60}')

    all_results = []

    for run_i in range(runs):
        print(f'\n--- Run {run_i+1}/{runs} ---')
        body = make_body(model, SPEED_PROMPT, stream=False, max_tokens=max_tokens)

        # 重试逻辑 for 429
        resp = None
        for retry in range(8):
            t_start = time.time()
            try:
                resp = requests.post(CHAT_URL, headers=HEADERS, json=body,
                                     timeout=(30, 300))
            except Exception as e:
                print(f'  ❌ 连接错误: {e}')
                resp = None
                break

            t_total = time.time() - t_start

            if resp.status_code == 429:
                wait = min(2 ** (retry + 1), 30)
                print(f'  ⚠️  429 限流, {wait}s 后重试 ({retry+1}/8)...')
                time.sleep(wait)
                continue
            elif resp.status_code != 200:
                print(f'  ❌ HTTP {resp.status_code}: {resp.text[:300]}')
                resp = None
                break
            else:
                break

        if resp is None or resp.status_code != 200:
            print(f'  跳过本轮')
            continue

        try:

            data = resp.json()
            content = data['choices'][0]['message'].get('content', '')
            usage = data.get('usage', {})
            output_tokens = usage.get('completion_tokens', 0)
            input_tokens = usage.get('prompt_tokens', 0)
            speed = output_tokens / t_total if t_total > 0 else 0

            result = {
                'total_time': t_total,
                'output_tokens': output_tokens,
                'input_tokens': input_tokens,
                'speed_tps': speed,
                'content_len': len(content),
            }
            all_results.append(result)

            print(f'  总耗时:         {t_total:.2f}s')
            print(f'  输出 tokens:    {output_tokens}')
            print(f'  输入 tokens:    {input_tokens}')
            print(f'  吞吐速度:       {speed:.1f} tokens/s (含网络延迟)')
            print(f'  内容长度:       {len(content)} chars')

        except Exception as e:
            print(f'  ❌ Error: {e}')
            continue

    return all_results


# ═══════════════════════════════════════════════════════
#  测试 3: RPM (每分钟请求数)
# ═══════════════════════════════════════════════════════

def test_rpm(model, concurrency=10, duration_sec=60, max_tokens=128):
    """并发发送请求，统计 60s 内能完成多少请求 → RPM"""
    print(f'\n{"="*60}')
    print(f'  📈 RPM 测试 — {model}')
    print(f'  并发数={concurrency}, 时长={duration_sec}s, max_tokens={max_tokens}')
    print(f'{"="*60}')

    success_count = 0
    error_count = 0
    rate_limit_count = 0
    latencies = []
    lock = Lock()
    t_global_start = time.time()

    def single_request(req_id):
        nonlocal success_count, error_count, rate_limit_count
        body = make_body(model, RPM_PROMPT, stream=False, max_tokens=max_tokens)
        t0 = time.time()
        try:
            resp = requests.post(CHAT_URL, headers=HEADERS, json=body,
                                 timeout=(15, 60))
            latency = time.time() - t0

            if resp.status_code == 429:
                with lock:
                    rate_limit_count += 1
                return 'rate_limited', latency
            elif resp.status_code != 200:
                with lock:
                    error_count += 1
                return 'error', latency
            else:
                data = resp.json()
                # 检查是否有 error
                if 'error' in data:
                    with lock:
                        error_count += 1
                    return 'error', latency
                with lock:
                    success_count += 1
                    latencies.append(latency)
                return 'ok', latency
        except Exception as e:
            with lock:
                error_count += 1
            return 'exception', time.time() - t0

    # 持续发请求直到时间用完
    req_id = 0
    total_submitted = 0
    batch_size = concurrency

    print(f'\n  开始测试...')

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        while time.time() - t_global_start < duration_sec:
            elapsed = time.time() - t_global_start
            remaining = duration_sec - elapsed
            if remaining < 2:
                break

            # 提交一批请求
            futures = []
            for _ in range(batch_size):
                if time.time() - t_global_start >= duration_sec:
                    break
                req_id += 1
                futures.append(pool.submit(single_request, req_id))
                total_submitted += 1

            # 等待这批完成
            for fut in as_completed(futures, timeout=max(remaining + 30, 60)):
                try:
                    status, lat = fut.result()
                except Exception as exc:
                    logger.debug('Future result error: %s', exc)

            elapsed = time.time() - t_global_start
            print(f'  [{elapsed:.0f}s] 成功={success_count} 错误={error_count} '
                  f'限流={rate_limit_count} 总提交={total_submitted}')

    t_total = time.time() - t_global_start

    print(f'\n{"─"*60}')
    print(f'  📊 RPM 结果')
    print(f'{"─"*60}')
    print(f'  测试时长:       {t_total:.1f}s')
    print(f'  总提交请求:     {total_submitted}')
    print(f'  成功请求:       {success_count}')
    print(f'  错误请求:       {error_count}')
    print(f'  限流 (429):     {rate_limit_count}')
    rpm = success_count / t_total * 60 if t_total > 0 else 0
    print(f'  ✅ 估算 RPM:    {rpm:.1f} requests/min')

    if latencies:
        latencies.sort()
        avg_lat = sum(latencies) / len(latencies)
        p50 = latencies[len(latencies) // 2]
        p90 = latencies[int(len(latencies) * 0.9)]
        p99 = latencies[int(len(latencies) * 0.99)]
        print(f'\n  延迟统计 (成功请求):')
        print(f'  平均:           {avg_lat:.2f}s')
        print(f'  P50:            {p50:.2f}s')
        print(f'  P90:            {p90:.2f}s')
        print(f'  P99:            {p99:.2f}s')
        print(f'  最快:           {latencies[0]:.2f}s')
        print(f'  最慢:           {latencies[-1]:.2f}s')

    return {
        'rpm': rpm,
        'success': success_count,
        'errors': error_count,
        'rate_limited': rate_limit_count,
        'avg_latency': sum(latencies) / len(latencies) if latencies else 0,
    }


# ═══════════════════════════════════════════════════════
#  先发一个探测请求确认模型可用
# ═══════════════════════════════════════════════════════

def probe_model(model, max_retries=5):
    """发一个简单请求确认模型可达，遇 429 会重试"""
    print(f'\n🔍 探测模型 {model} ...')
    body = make_body(model, "Hi, respond with just 'OK'.", stream=False, max_tokens=16)
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(CHAT_URL, headers=HEADERS, json=body, timeout=(15, 60))
            if resp.status_code == 200:
                data = resp.json()
                content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
                print(f'  ✅ 模型可用! 响应: "{content[:100]}"')
                return True
            elif resp.status_code == 429:
                wait = min(2 ** attempt, 30)
                print(f'  ⚠️  429 限流 (attempt {attempt}/{max_retries})，{wait}s 后重试...')
                time.sleep(wait)
                continue
            else:
                print(f'  ❌ HTTP {resp.status_code}: {resp.text[:300]}')
                return False
        except Exception as e:
            print(f'  ❌ 连接失败: {e}')
            return False
    print(f'  ⚠️  持续 429，但模型路由存在，继续测试...')
    return True  # 429 说明模型存在，只是限流


# ═══════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Benchmark LLM model speed & RPM')
    parser.add_argument('--model', default=DEFAULT_MODEL, help='Model name')
    parser.add_argument('--speed-only', action='store_true', help='Only run speed test')
    parser.add_argument('--rpm-only', action='store_true', help='Only run RPM test')
    parser.add_argument('--max-tokens', type=int, default=2048, help='Max tokens for speed test')
    parser.add_argument('--runs', type=int, default=3, help='Number of speed test runs')
    parser.add_argument('--concurrency', type=int, default=10, help='Concurrency for RPM test')
    parser.add_argument('--duration', type=int, default=60, help='Duration (seconds) for RPM test')
    args = parser.parse_args()

    print(f'🎯 Benchmark: {args.model}')
    print(f'   API: {CHAT_URL}')

    # 先探测
    if not probe_model(args.model):
        print('\n⛔ 模型不可用，退出。')
        sys.exit(1)

    run_speed = not args.rpm_only
    run_rpm = not args.speed_only

    # 速度测试
    if run_speed:
        test_speed_streaming(args.model, max_tokens=args.max_tokens, runs=args.runs)
        test_speed_non_streaming(args.model, max_tokens=args.max_tokens, runs=args.runs)

    # RPM 测试
    if run_rpm:
        test_rpm(args.model, concurrency=args.concurrency,
                 duration_sec=args.duration, max_tokens=128)

    print(f'\n✅ 测试完成!')


if __name__ == '__main__':
    main()
