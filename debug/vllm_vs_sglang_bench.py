#!/usr/bin/env python3
"""
vLLM vs SGLang comparison benchmark
====================================
Configure endpoints via NODES dict below or env vars.
"""

import json, time, sys, os, statistics
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError

# ─── 配置 ────────────────────────────────────────────────
NODES = OrderedDict([
    ("vLLM", {
        "url": os.environ.get("VLLM_URL", "http://localhost:8080/v1/chat/completions"),
        "model": os.environ.get("VLLM_MODEL", "gpt-4o"),
        "tag": "vLLM",
    }),
    ("SGLang", {
        "url": os.environ.get("SGLANG_URL", "http://localhost:8081/v1/chat/completions"),
        "model": os.environ.get("SGLANG_MODEL", "gpt-4o"),
        "tag": "SGLang",
    }),
])

# 测试 prompt 集合
PROMPTS = [
    ("极短-打招呼",    "hi"),
    ("短-数学",        "1+1等于几？只回答数字"),
    ("中-翻译",        "Translate to English: 今天天气真好，我想出去散步"),
    ("中-编程",        "用python写一个快速排序，不要解释，只输出代码"),
    ("长-分析",        "请分析中国2024年GDP增长趋势，从以下角度：消费、投资、出口，每个角度2-3句话"),
]

TIMEOUT = 120

# ─── 工具函数 ────────────────────────────────────────────

def req(url, model, prompt, max_tokens, temperature=0.6, timeout=TIMEOUT):
    """单请求: 返回 (耗时s, 输出token数, 输出文本, usage_dict)"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    r = Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    resp = urlopen(r, timeout=timeout)
    data = json.loads(resp.read())
    elapsed = time.perf_counter() - t0

    usage = data.get("usage", {})
    comp_tokens = usage.get("completion_tokens", 0)
    choice = data["choices"][0]["message"]
    text = choice.get("content") or choice.get("reasoning") or ""
    return elapsed, comp_tokens, text.strip(), usage


def req_stream(url, model, prompt, max_tokens, temperature=0.6, timeout=TIMEOUT):
    """流式请求: 返回 (TTFT, 总耗时, chunk数, 文本长度)"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }).encode()
    r = Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    resp = urlopen(r, timeout=timeout)

    ttft = None
    chunks = 0
    total_text = ""
    buffer = ""

    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
                delta = d["choices"][0].get("delta", {})
                content = (delta.get("content") or delta.get("reasoning")
                           or delta.get("reasoning_content") or "")
                if content:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    chunks += 1
                    total_text += content
            except Exception:
                pass

    total_time = time.perf_counter() - t0
    if ttft is None:
        ttft = total_time
    return ttft, total_time, chunks, len(total_text)


def concurrent_test(url, model, prompt, max_tokens, concurrency, num_requests):
    """并发测试: 返回 (墙钟时间, [(耗时,tok数),...])"""
    results = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [pool.submit(req, url, model, prompt, max_tokens)
                for _ in range(num_requests)]
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                results.append((None, 0, str(e), {}))
    return time.perf_counter() - t0, results


def pct(sorted_list, p):
    idx = min(int(len(sorted_list) * p), len(sorted_list) - 1)
    return sorted_list[idx]


# ─── 主测试 ──────────────────────────────────────────────

def main():
    W = 120

    print("=" * W)
    print("GLM-5-FP8   vLLM  vs  SGLang  对比压测".center(W))
    print("=" * W)

    # ── Phase 0: 预热 ──
    print("\n🔥 Phase 0: 预热 (每节点2请求)")
    for name, cfg in NODES.items():
        for i in range(2):
            try:
                el, tk, txt, _ = req(cfg["url"], cfg["model"], "hello", 3)
                if i == 1:
                    print(f"  {name}: {el:.2f}s, {tk}tok → {txt[:50]}")
            except Exception as e:
                print(f"  {name}: ❌ {e}")

    # ── Phase 1: 单请求延迟对比 ──
    print(f"\n{'━' * W}")
    print("📊 Phase 1: 单请求延迟对比  (max_tokens=32, temperature=0.6)")
    print(f"{'━' * W}")

    hdr = (f"  {'Prompt':<15}│{'vLLM 延迟':>10} {'tok':>4} {'tok/s':>7}"
           f"│{'SGLang 延迟':>12} {'tok':>4} {'tok/s':>7}"
           f"│{'延迟胜出':>12}")
    print(hdr)
    print("  " + "─" * (W - 4))

    node_lats = {n: [] for n in NODES}
    node_tps  = {n: [] for n in NODES}

    for pname, ptxt in PROMPTS:
        res = {}
        for name, cfg in NODES.items():
            try:
                el, tk, txt, usg = req(cfg["url"], cfg["model"], ptxt, 32)
                tps = tk / el if el > 0 else 0
                res[name] = (el, tk, tps)
                node_lats[name].append(el)
                node_tps[name].append(tps)
            except Exception as e:
                res[name] = (None, 0, 0)

        parts = []
        for name in NODES:
            el, tk, tps = res[name]
            if el is not None:
                parts.append(f"{el:>9.2f}s {tk:>4} {tps:>6.1f}")
            else:
                parts.append(f"{'FAIL':>9} {'--':>4} {'--':>6}")

        times_ok = [(n, res[n][0]) for n in NODES if res[n][0] is not None]
        if len(times_ok) == 2:
            w = min(times_ok, key=lambda x: x[1])
            r = max(t for _, t in times_ok) / max(min(t for _, t in times_ok), 0.001)
            wl = f"{NODES[w[0]]['tag']} {r:.1f}x"
        else:
            wl = "--"

        print(f"  {pname:<15}│{'│'.join(parts)}│{wl:>12}")

    print("  " + "─" * (W - 4))
    for name in NODES:
        lats = node_lats[name]
        tl = node_tps[name]
        if lats:
            print(f"  {NODES[name]['tag']:>6}: 平均延迟={statistics.mean(lats):.2f}s  "
                  f"中位延迟={statistics.median(lats):.2f}s  "
                  f"平均tok/s={statistics.mean(tl):.1f}")

    # ── Phase 2: 流式 TTFT ──
    print(f"\n{'━' * W}")
    print("🚀 Phase 2: 流式首Token延迟(TTFT)对比  (max_tokens=64)")
    print(f"{'━' * W}")

    hdr2 = (f"  {'Prompt':<15}│{'vLLM TTFT':>10} {'总耗时':>8} {'chunks':>7}"
            f"│{'SGLang TTFT':>12} {'总耗时':>8} {'chunks':>7}"
            f"│{'TTFT胜出':>12}")
    print(hdr2)
    print("  " + "─" * (W - 4))

    for pname, ptxt in PROMPTS[:4]:
        res = {}
        for name, cfg in NODES.items():
            try:
                ttft, total, chunks, tlen = req_stream(
                    cfg["url"], cfg["model"], ptxt, 64)
                res[name] = (ttft, total, chunks)
            except Exception as e:
                res[name] = (None, None, 0)

        parts = []
        for name in NODES:
            ttft, total, chunks = res[name]
            if ttft is not None:
                parts.append(f"{ttft * 1000:>9.0f}ms {total:>7.2f}s {chunks:>7}")
            else:
                parts.append(f"{'FAIL':>9} {'--':>7} {'--':>7}")

        ttfts = [(n, res[n][0]) for n in NODES if res[n][0] is not None]
        if len(ttfts) == 2:
            w = min(ttfts, key=lambda x: x[1])
            r = max(t for _, t in ttfts) / max(min(t for _, t in ttfts), 0.001)
            wl = f"{NODES[w[0]]['tag']} {r:.1f}x"
        else:
            wl = "--"

        print(f"  {pname:<15}│{'│'.join(parts)}│{wl:>12}")

    # ── Phase 3: 并发吞吐 ──
    print(f"\n{'━' * W}")
    print("⚡ Phase 3: 并发吞吐对比  (prompt='写一首关于AI的五言绝句', max_tokens=64)")
    print(f"{'━' * W}")

    conc_levels = [1, 2, 4, 8, 16]
    conc_prompt = "写一首关于AI的五言绝句"
    conc_maxt = 64

    hdr3 = (f"  {'C':>3}│"
            f"{'vLLM墙钟':>8} {'ok':>3} {'req/s':>6} {'tok/s':>6} {'P50':>7} {'P99':>7}"
            f"│{'SGLang墙钟':>10} {'ok':>3} {'req/s':>6} {'tok/s':>6} {'P50':>7} {'P99':>7}"
            f"│{'tok/s胜':>10}")
    print(hdr3)
    print("  " + "─" * (W - 4))

    tput_summary = {n: [] for n in NODES}

    for conc in conc_levels:
        num_req = max(conc * 2, 4)
        if conc >= 16:
            num_req = conc

        row_parts = []
        conc_res = {}

        for name, cfg in NODES.items():
            try:
                wall, results = concurrent_test(
                    cfg["url"], cfg["model"], conc_prompt, conc_maxt, conc, num_req)
                lats = sorted([r[0] for r in results if r[0] is not None])
                total_tok = sum(r[1] for r in results if r[0] is not None)
                ok = len(lats)

                if lats:
                    rps = ok / wall
                    tps = total_tok / wall
                    p50 = pct(lats, 0.5)
                    p99 = pct(lats, 0.99)
                    conc_res[name] = (wall, ok, rps, tps, p50, p99)
                    tput_summary[name].append((conc, tps, rps))
                    row_parts.append(
                        f"{wall:>7.1f}s {ok:>3} {rps:>5.2f} {tps:>5.1f} {p50:>6.2f}s {p99:>6.2f}s")
                else:
                    conc_res[name] = None
                    row_parts.append(f"{'FAIL':>7} {'':>3} {'':>5} {'':>5} {'':>6} {'':>6}")
            except Exception as e:
                conc_res[name] = None
                row_parts.append(f"{'ERR':>7} {'':>3} {'':>5} {'':>5} {'':>6} {'':>6}")

        tps_vals = [(n, conc_res[n][3]) for n in NODES if conc_res.get(n)]
        if len(tps_vals) == 2:
            w = max(tps_vals, key=lambda x: x[1])
            r = max(t for _, t in tps_vals) / max(min(t for _, t in tps_vals), 0.01)
            wl = f"{NODES[w[0]]['tag']} {r:.1f}x"
        else:
            wl = "--"

        print(f"  {conc:>3}│{'│'.join(row_parts)}│{wl:>10}")

    # ── Phase 4: 长输出 ──
    print(f"\n{'━' * W}")
    print("📝 Phase 4: 长输出对比  (max_tokens=256)")
    print(f"{'━' * W}")

    long_prompt = "请详细介绍Python的GIL机制，包括：什么是GIL、为什么存在、有什么影响、如何规避"
    for name, cfg in NODES.items():
        try:
            el, tk, txt, usg = req(cfg["url"], cfg["model"], long_prompt, 256, timeout=180)
            tps = tk / el if el > 0 else 0
            print(f"  {NODES[name]['tag']:>6}: {el:.2f}s | {tk} tokens | {tps:.1f} tok/s"
                  f" | prompt_tok={usg.get('prompt_tokens',0)}")
            print(f"         预览: {txt[:100]}…")
        except Exception as e:
            print(f"  {NODES[name]['tag']:>6}: ❌ {e}")

    # ── 最终汇总 ──
    print(f"\n{'=' * W}")
    print("📋 最终汇总".center(W))
    print(f"{'=' * W}")

    print(f"\n  {'指标':<20}│{'vLLM (特殊节点)':>20}│{'SGLang (画布节点)':>20}│{'结论':>15}")
    print(f"  {'─'*20}┼{'─'*20}┼{'─'*20}┼{'─'*15}")

    for name in NODES:
        tag = NODES[name]["tag"]
        lats = node_lats[name]
        tl = node_tps[name]
        if lats:
            avg_lat = f"{statistics.mean(lats):.2f}s"
            med_lat = f"{statistics.median(lats):.2f}s"
            avg_tps = f"{statistics.mean(tl):.1f} tok/s"
        else:
            avg_lat = med_lat = avg_tps = "N/A"

    # 单独打印每行
    names = list(NODES.keys())
    def _val(name, fn):
        lats = node_lats[name]; tl = node_tps[name]
        if not lats: return "N/A"
        return fn(lats, tl)

    def _better(v1, v2, lower_is_better=True):
        try:
            a, b = float(v1.rstrip("s").rstrip(" tok/")), float(v2.rstrip("s").rstrip(" tok/"))
            if lower_is_better: return "← vLLM" if a < b else "SGLang →"
            else: return "← vLLM" if a > b else "SGLang →"
        except: return "~"

    v1_avg = _val(names[0], lambda l,t: f"{statistics.mean(l):.2f}s")
    v2_avg = _val(names[1], lambda l,t: f"{statistics.mean(l):.2f}s")
    print(f"  {'平均延迟(32tok)':<20}│{v1_avg:>20}│{v2_avg:>20}│{_better(v1_avg,v2_avg):>15}")

    v1_tps = _val(names[0], lambda l,t: f"{statistics.mean(t):.1f}")
    v2_tps = _val(names[1], lambda l,t: f"{statistics.mean(t):.1f}")
    print(f"  {'平均tok/s(32tok)':<20}│{v1_tps+' tok/s':>20}│{v2_tps+' tok/s':>20}│{_better(v1_tps,v2_tps,False):>15}")

    # 吞吐扩展
    print(f"\n  📈 并发扩展 tok/s:")
    for name in NODES:
        tag = NODES[name]["tag"]
        vals = tput_summary[name]
        if vals:
            line = " → ".join(f"C{c}:{tps:.1f}" for c, tps, _ in vals)
            print(f"    {tag:>6}: {line}")

    print(f"\n{'=' * W}")
    print("测试完成!".center(W))
    print(f"{'=' * W}\n")


if __name__ == "__main__":
    main()
