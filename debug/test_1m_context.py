#!/usr/bin/env python3
"""test_1m_context.py — 测试 LLM 网关的 Opus 4.6 是否原生支持 1M 上下文

测试策略:
  1. 发送一个 ~250K token 的请求 (超过旧 200K 限制)，看是否成功
  2. 在长上下文开头埋入一个 "needle"，在末尾让模型找出来 (Needle-in-a-Haystack)
  3. 对比 aws / vertex 两个部署
  4. 不需要任何额外 header (anthropic-beta 等)

结论预期:
  - 如果成功 → 网关已原生支持 1M，无需额外 header
  - 如果 400/413 → 网关可能还未升级，需要 anthropic-beta header 或联系平台方

用法: python debug/test_1m_context.py
"""

import json, time, sys, os, uuid, hashlib, requests

# ── 配置 ──
BASE_URL = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1')
API_KEYS = os.environ.get('LLM_API_KEYS', '').split(',')
if not API_KEYS or API_KEYS == ['']:
    raise RuntimeError('Set LLM_API_KEYS env var (comma-separated) to run this test')
API_KEY = API_KEYS[0].strip()

# 要测试的模型列表 (aws=Bedrock代理, vertex=Google代理)
MODELS_TO_TEST = [
    'aws.claude-opus-4.6',
    'aws.claude-opus-4.6-b',
    'vertex.claude-opus-4.6',
    'aws.claude-sonnet-4.6',
]

CHAT_URL = f'{BASE_URL}/chat/completions'
NO_PROXY = {'http': '', 'https': '', 'no_proxy': '*'}

# ── 生成填充文本 ──
def make_padding(target_tokens: int) -> str:
    """生成大约 target_tokens 个 token 的填充文本。
    
    英文文本大约 1 token ≈ 4 chars (包含空格)。
    我们用重复段落 + 随机数来避免极端压缩。
    """
    # 每个 token ≈ 4 chars
    target_chars = target_tokens * 4
    
    # 基础段落 (~200 chars each, ~50 tokens)
    paragraphs = [
        f"Section {{n}}: The quick brown fox jumps over the lazy dog. "
        f"Pack my box with five dozen liquor jugs. "
        f"How vexingly quick daft zebras jump. "
        f"The five boxing wizards jump quickly. "
        f"Sphinx of black quartz, judge my vow. [ref-{{n}}]",
        
        f"Chapter {{n}}: Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        f"Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. "
        f"Ut enim ad minim veniam, quis nostrud exercitation ullamco. "
        f"Duis aute irure dolor in reprehenderit. [data-{{n}}]",
        
        f"Article {{n}}: In a hole in the ground there lived a hobbit. "
        f"Not a nasty, dirty, wet hole, filled with the ends of worms. "
        f"Nor yet a dry, bare, sandy hole with nothing to sit down on. "
        f"It was a hobbit-hole, and that means comfort. [item-{{n}}]",
    ]
    
    chunks = []
    n = 0
    total = 0
    while total < target_chars:
        p = paragraphs[n % len(paragraphs)].replace('{n}', str(n))
        chunks.append(p)
        total += len(p) + 1  # +1 for newline
        n += 1
    
    return '\n'.join(chunks)


def test_model(model: str, target_tokens: int, needle: str, needle_value: str):
    """测试单个模型是否支持大于 200K 的上下文。
    
    Returns: dict with results
    """
    print(f"\n{'='*70}")
    print(f"  模型: {model}")
    print(f"  目标上下文: ~{target_tokens:,} tokens")
    print(f"{'='*70}")
    
    # 生成填充
    padding = make_padding(target_tokens)
    estimated_tokens = len(padding) // 4
    
    # 在填充开头埋入 needle
    needle_text = f"\n\n[SECRET NEEDLE] The {needle} is: {needle_value}\n\n"
    
    # 系统消息 + 用户消息
    system_msg = (
        "You are a helpful assistant. You must carefully read ALL the text provided "
        "and answer questions about it precisely."
    )
    
    user_content = (
        f"I'm going to give you a very long document. Somewhere in it, there is a "
        f"secret needle containing a special value for '{needle}'. "
        f"Please read everything carefully.\n\n"
        f"--- DOCUMENT START ---\n"
        f"{needle_text}"
        f"{padding}\n"
        f"--- DOCUMENT END ---\n\n"
        f"Question: What is the value of '{needle}' mentioned in the [SECRET NEEDLE] section? "
        f"Reply with ONLY the value, nothing else."
    )
    
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_content},
    ]
    
    # 估算 token 数
    total_chars = len(system_msg) + len(user_content)
    est_input_tokens = total_chars // 4
    
    print(f"  填充文本: {len(padding):,} chars")
    print(f"  总输入: {total_chars:,} chars ≈ {est_input_tokens:,} tokens (估算)")
    
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": 100,         # 只需要短回复
        "temperature": 0,
        "stream": False,
    }
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {API_KEY}',
        'M-TraceId': uuid.uuid4().hex,
    }
    # 注意: 故意不加 anthropic-beta header，测试原生支持
    
    print(f"\n  ⏳ 发送请求 (无 anthropic-beta header)...")
    t0 = time.time()
    
    try:
        resp = requests.post(
            CHAT_URL,
            headers=headers,
            json=body,
            timeout=(60, 300),  # connect=60s, read=300s (长上下文需要更长处理时间)
            proxies=NO_PROXY,
        )
        elapsed = time.time() - t0
        
        print(f"  HTTP {resp.status_code} ({elapsed:.1f}s)")
        
        if resp.status_code == 200:
            data = resp.json()
            choices = data.get('choices', [])
            usage = data.get('usage', {})
            
            content = choices[0]['message']['content'] if choices else '<no content>'
            prompt_tokens = usage.get('prompt_tokens', 0)
            completion_tokens = usage.get('completion_tokens', 0)
            
            # 检查 needle 是否被正确找到
            found_needle = needle_value.lower() in content.lower()
            
            print(f"\n  ✅ 成功!")
            print(f"  prompt_tokens:     {prompt_tokens:,}")
            print(f"  completion_tokens: {completion_tokens:,}")
            print(f"  模型回复: {content[:200]}")
            print(f"  Needle 测试: {'✅ PASS' if found_needle else '❌ FAIL'} "
                  f"(期望包含 '{needle_value}')")
            
            return {
                'model': model,
                'status': 'SUCCESS',
                'http_code': 200,
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'elapsed_s': round(elapsed, 1),
                'needle_found': found_needle,
                'content': content[:500],
                'supports_1m': True,
                'extra_header_needed': False,
            }
        else:
            error_text = resp.text[:1000]
            print(f"\n  ❌ 失败!")
            print(f"  错误: {error_text}")
            
            # 分析错误类型
            needs_header = False
            if resp.status_code in (400, 413):
                if 'context' in error_text.lower() or 'token' in error_text.lower() or 'length' in error_text.lower():
                    needs_header = True
                    print(f"\n  💡 可能原因: 网关不支持 >200K, 可能需要 anthropic-beta: context-1m-2025-05-14 header")
            
            return {
                'model': model,
                'status': 'FAILED',
                'http_code': resp.status_code,
                'error': error_text,
                'elapsed_s': round(elapsed, 1),
                'supports_1m': False,
                'extra_header_needed': needs_header,
            }
            
    except requests.exceptions.Timeout:
        elapsed = time.time() - t0
        print(f"\n  ⏰ 超时 ({elapsed:.1f}s)")
        return {
            'model': model,
            'status': 'TIMEOUT',
            'elapsed_s': round(elapsed, 1),
            'supports_1m': 'unknown',
        }
    except Exception as e:
        elapsed = time.time() - t0
        print(f"\n  💥 异常: {e}")
        return {
            'model': model,
            'status': 'ERROR',
            'error': str(e),
            'elapsed_s': round(elapsed, 1),
            'supports_1m': 'unknown',
        }


def test_with_beta_header(model: str, target_tokens: int):
    """如果无 header 失败，尝试加上 anthropic-beta header 再测一次。"""
    print(f"\n{'='*70}")
    print(f"  🔄 重测 (带 anthropic-beta header): {model}")
    print(f"{'='*70}")
    
    padding = make_padding(target_tokens)
    user_content = (
        f"This is a long context test. {padding}\n\n"
        f"What is 2+2? Reply with just the number."
    )
    
    body = {
        "model": model,
        "messages": [
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 50,
        "temperature": 0,
        "stream": False,
    }
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {API_KEY}',
        'M-TraceId': uuid.uuid4().hex,
        # ★ 关键: 加上 1M 上下文 beta header
        'anthropic-beta': 'context-1m-2025-05-14',
    }
    
    print(f"  ⏳ 发送请求 (带 anthropic-beta: context-1m-2025-05-14)...")
    t0 = time.time()
    
    try:
        resp = requests.post(
            CHAT_URL, headers=headers, json=body,
            timeout=(60, 300), proxies=NO_PROXY,
        )
        elapsed = time.time() - t0
        print(f"  HTTP {resp.status_code} ({elapsed:.1f}s)")
        
        if resp.status_code == 200:
            data = resp.json()
            usage = data.get('usage', {})
            print(f"  ✅ 带 header 成功! prompt_tokens={usage.get('prompt_tokens', 0):,}")
            return True
        else:
            print(f"  ❌ 带 header 也失败: {resp.text[:500]}")
            return False
    except Exception as e:
        print(f"  💥 异常: {e}")
        return False


def main():
    print("=" * 70)
    print("  Claude Opus 4.6 — 1M Context Window 测试")
    print("  Gateway: " + BASE_URL)
    print("=" * 70)
    print()
    print("背景知识:")
    print("  • 2026-03-13 Anthropic 宣布 1M context GA, 无需 beta header")
    print("  • Opus 4.6 / Sonnet 4.6 均支持, 无额外加价")
    print("  • 旧的 beta header: anthropic-beta: context-1m-2025-05-14")
    print("  • 如果网关已升级 → 直接支持; 如果未升级 → 可能需要 header 或被截断")
    print()
    
    # Needle: 在开头埋一个随机值, 让模型从 250K tokens 的上下文中找出来
    needle = "magic_password"
    needle_value = f"unicorn-{uuid.uuid4().hex[:8]}"
    
    # 测试规模: ~250K tokens (超过旧 200K 限制, 但不会太大以至于超时)
    TARGET_TOKENS = 250_000
    
    # 如果想快速测试，可以用小一点的值
    if '--quick' in sys.argv:
        TARGET_TOKENS = 50_000
        print("  [--quick 模式: 仅测试 50K tokens, 不测试 >200K 边界]")
        print()
    
    # 如果指定了模型, 只测那一个
    test_models = MODELS_TO_TEST
    for arg in sys.argv[1:]:
        if arg.startswith('--model='):
            test_models = [arg.split('=', 1)[1]]
    
    results = []
    for model in test_models:
        result = test_model(model, TARGET_TOKENS, needle, needle_value)
        results.append(result)
        
        # 如果失败且可能是 header 问题, 加 header 重试
        if result.get('extra_header_needed'):
            beta_ok = test_with_beta_header(model, TARGET_TOKENS)
            result['beta_header_works'] = beta_ok
    
    # ── 汇总 ──
    print("\n")
    print("=" * 70)
    print("  汇总")
    print("=" * 70)
    print()
    print(f"  {'模型':<30} {'状态':<10} {'prompt_tokens':<15} {'耗时':<8} {'1M支持'}")
    print(f"  {'-'*30} {'-'*10} {'-'*15} {'-'*8} {'-'*10}")
    
    for r in results:
        model = r['model']
        status = r['status']
        ptok = f"{r.get('prompt_tokens', '-'):,}" if r.get('prompt_tokens') else '-'
        elapsed = f"{r['elapsed_s']}s"
        support = '✅' if r.get('supports_1m') == True else ('❓' if r.get('supports_1m') == 'unknown' else '❌')
        print(f"  {model:<30} {status:<10} {ptok:<15} {elapsed:<8} {support}")
    
    # ── 结论 ──
    all_success = all(r['status'] == 'SUCCESS' for r in results)
    any_header_needed = any(r.get('extra_header_needed') for r in results)
    
    print()
    if all_success:
        print("  🎉 结论: 所有模型原生支持 >200K tokens, 无需额外 header!")
        print("  Gateway supports 1M context window natively.")
    elif any_header_needed:
        print("  ⚠️  结论: 部分/全部模型需要 anthropic-beta header")
        print("  建议在 llm_client.py 的 _headers() 中添加:")
        print("    'anthropic-beta': 'context-1m-2025-05-14'")
    else:
        print("  ⚠️  结论: 测试结果不明确, 请查看上面的详细错误信息")
    
    print()


if __name__ == '__main__':
    main()
