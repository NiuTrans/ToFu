#!/usr/bin/env python3
"""
SGLang GLM-5 吞吐量压测 — 128 并发
====================================
目标：测量 http://33.235.248.105:8080/v1/chat/completions 在 128 并发下的
      总吞吐 tok/s、req/s、延迟分布（含 thinking token）。

GLM-5 默认开启 thinking，completion_tokens = thinking + output tokens 总数。
"""

import json, time, sys, statistics, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError

# ═══════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════
URL = "http://33.235.248.105:8080/v1/chat/completions"
MODEL = "glm5"
TIMEOUT = 300  # 单请求超时

# 多样化 prompt，模拟真实负载
PROMPTS = [
    "1+1等于几？",
    "请简要解释什么是量子纠缠",
    "用Python写一个冒泡排序",
    "翻译成英文：今天天气真好，我想出去散步",
    "中国最长的河流是什么？为什么？",
    "请比较TCP和UDP的区别",
    "什么是梯度下降？一句话解释",
    "写一首关于春天的五言绝句",
    "解释什么是哈希表，时间复杂度是多少",
    "请推荐3本经典的计算机科学书籍",
    "简述HTTP和HTTPS的区别",
    "什么是MapReduce？简要说明",
    "Explain the difference between a stack and a queue",
    "Write a Python function to check if a string is a palindrome",
    "What is the time complexity of binary search?",
    "请用一句话解释相对论",
]

W = 90  # 输出宽度


# ═══════════════════════════════════════════════════════════════
#  核心请求
# ═══════════════════════════════════════════════════════════════
def do_request(prompt, max_tokens=512):
    """
    同步请求，返回 dict:
      ok, latency, completion_tokens, prompt_tokens, total_tokens,
      reasoning_len, content_len, finish_reason, error
    """
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.6,
    }).encode()
    req = Request(URL, data=body, headers={"Content-Type": "application/json"})

    t0 = time.perf_counter()
    try:
        resp = urlopen(req, timeout=TIMEOUT)
        data = json.loads(resp.read())
        latency = time.perf_counter() - t0

        ch = data["choices"][0]
        msg = ch["message"]
        usage = data.get("usage", {})

        reasoning_text = msg.get("reasoning_content") or ""
        content_text = msg.get("content") or ""

        return {
            "ok": True,
            "latency": latency,
            "completion_tokens": usage.get("completion_tokens", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "reasoning_len": len(reasoning_text),
            "content_len": len(content_text),
            "finish_reason": ch.get("finish_reason", "?"),
            "error": None,
        }
    except Exception as e:
        return {
            "ok": False,
            "latency": time.perf_counter() - t0,
            "completion_tokens": 0,
            "prompt_tokens": 0,
            "total_tokens": 0,
            "reasoning_len": 0,
            "content_len": 0,
            "finish_reason": "error",
            "error": str(e)[:120],
        }


def percentile(sorted_list, p):
    if not sorted_list:
        return 0
    idx = min(int(len(sorted_list) * p), len(sorted_list) - 1)
    return sorted_list[idx]


# ═══════════════════════════════════════════════════════════════
#  Phase 0: 预热 & 验证
# ═══════════════════════════════════════════════════════════════
def phase0_warmup():
    print(f"\n{'=' * W}")
    print("  Phase 0: 预热 & 连通性验证")
    print(f"{'=' * W}")

    for i in range(3):
        r = do_request("hello", max_tokens=32)
        if r["ok"]:
            print(f"  预热 #{i+1}: ✅ {r['latency']:.2f}s | "
                  f"comp_tok={r['completion_tokens']} | "
                  f"reasoning_chars={r['reasoning_len']} content_chars={r['content_len']} | "
                  f"{r['finish_reason']}")
        else:
            print(f"  预热 #{i+1}: ❌ {r['error']}")
            return False
    return True


# ═══════════════════════════════════════════════════════════════
#  Phase 1: 单请求基线
# ═══════════════════════════════════════════════════════════════
def phase1_baseline():
    print(f"\n{'=' * W}")
    print("  Phase 1: 单请求基线 (串行, max_tokens=512)")
    print(f"{'=' * W}")

    results = []
    for i, p in enumerate(PROMPTS[:6]):
        r = do_request(p, max_tokens=512)
        results.append(r)
        if r["ok"]:
            tps = r["completion_tokens"] / r["latency"] if r["latency"] > 0 else 0
            print(f"  [{i+1}] {p[:30]:30s} | {r['latency']:5.2f}s | "
                  f"comp={r['completion_tokens']:4d}tok | "
                  f"{tps:5.1f} tok/s | {r['finish_reason']}")
        else:
            print(f"  [{i+1}] {p[:30]:30s} | ❌ {r['error']}")

    ok_results = [r for r in results if r["ok"]]
    if ok_results:
        avg_lat = statistics.mean([r["latency"] for r in ok_results])
        avg_tok = statistics.mean([r["completion_tokens"] for r in ok_results])
        avg_tps = statistics.mean([r["completion_tokens"] / r["latency"] for r in ok_results])
        print(f"\n  基线汇总: avg_lat={avg_lat:.2f}s  avg_tok={avg_tok:.0f}  avg_tok/s={avg_tps:.1f}")
    return ok_results


# ═══════════════════════════════════════════════════════════════
#  Phase 2: 并发梯度 → 128
# ═══════════════════════════════════════════════════════════════
def phase2_concurrency_ladder():
    print(f"\n{'=' * W}")
    print("  Phase 2: 并发梯度测试 (max_tokens=512, thinking ON)")
    print(f"{'=' * W}")

    conc_levels = [1, 4, 8, 16, 32, 64, 128]
    all_results = {}

    header = (f"  {'C':>4} {'N':>5} │ {'Wall':>7} │ {'tok/s':>8} {'req/s':>7} │ "
              f"{'AvgLat':>7} {'P50':>7} {'P90':>7} {'P99':>7} │ "
              f"{'AvgTok':>6} {'Err':>4}")
    print(header)
    print(f"  {'─' * (W - 4)}")

    for conc in conc_levels:
        # 请求数: 至少并发的 3 倍，确保有足够数据
        if conc <= 8:
            n_reqs = max(conc * 4, 8)
        elif conc <= 32:
            n_reqs = conc * 3
        elif conc <= 64:
            n_reqs = conc * 2
        else:  # 128
            n_reqs = 256  # 128 并发，跑 256 请求

        # 分配 prompt
        task_prompts = (PROMPTS * ((n_reqs // len(PROMPTS)) + 1))[:n_reqs]

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=conc) as pool:
            futures = [pool.submit(do_request, p, 512) for p in task_prompts]
            results = [f.result() for f in futures]
        wall = time.perf_counter() - t0

        ok_results = [r for r in results if r["ok"]]
        err_count = n_reqs - len(ok_results)

        if ok_results:
            lats = sorted([r["latency"] for r in ok_results])
            total_comp_tok = sum(r["completion_tokens"] for r in ok_results)
            avg_tok = statistics.mean([r["completion_tokens"] for r in ok_results])

            tok_s = total_comp_tok / wall
            req_s = len(ok_results) / wall
            avg_lat = statistics.mean(lats)
            p50 = percentile(lats, 0.50)
            p90 = percentile(lats, 0.90)
            p99 = percentile(lats, 0.99)

            print(f"  {conc:>4} {n_reqs:>5} │ {wall:6.1f}s │ "
                  f"{tok_s:>7.1f} {req_s:>6.2f} │ "
                  f"{avg_lat:>6.2f}s {p50:>6.2f}s {p90:>6.2f}s {p99:>6.2f}s │ "
                  f"{avg_tok:>5.0f} {err_count:>4}")

            all_results[conc] = {
                "n": n_reqs, "wall": wall, "tok_s": tok_s, "req_s": req_s,
                "avg_lat": avg_lat, "p50": p50, "p90": p90, "p99": p99,
                "avg_tok": avg_tok, "total_comp_tok": total_comp_tok,
                "ok": len(ok_results), "err": err_count,
            }
        else:
            print(f"  {conc:>4} {n_reqs:>5} │ {wall:6.1f}s │ ALL FAILED ({err_count} errors)")
            # 打印前几个错误
            for r in results[:3]:
                print(f"         ❌ {r['error']}")
            all_results[conc] = None

    return all_results


# ═══════════════════════════════════════════════════════════════
#  Phase 3: 128 并发 持续压测 (稳态吞吐)
# ═══════════════════════════════════════════════════════════════
def phase3_sustained_128():
    print(f"\n{'=' * W}")
    print("  Phase 3: 128 并发持续压测 — 稳态吞吐 (max_tokens=512, ~300+ reqs)")
    print(f"{'=' * W}")

    CONC = 128
    N_REQS = 384  # 128 * 3，确保每个 worker 跑多轮

    task_prompts = (PROMPTS * ((N_REQS // len(PROMPTS)) + 1))[:N_REQS]

    lats = []
    comp_tokens_list = []
    errs = 0
    done = 0
    total_comp_tok = 0

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONC) as pool:
        futures = [pool.submit(do_request, p, 512) for p in task_prompts]
        for f in as_completed(futures):
            r = f.result()
            done += 1
            if r["ok"]:
                lats.append(r["latency"])
                comp_tokens_list.append(r["completion_tokens"])
                total_comp_tok += r["completion_tokens"]
            else:
                errs += 1

            # 进度报告
            if done % 50 == 0 or done == N_REQS:
                elapsed = time.perf_counter() - t0
                cur_tps = total_comp_tok / elapsed if elapsed > 0 else 0
                print(f"    进度: {done:>4}/{N_REQS} | "
                      f"elapsed={elapsed:>6.1f}s | "
                      f"tok/s={cur_tps:>7.1f} | "
                      f"err={errs}", flush=True)

    wall = time.perf_counter() - t0
    lats_sorted = sorted(lats)

    print(f"\n  ┌{'─' * (W - 6)}┐")
    print(f"  │{'128 并发持续压测结果':^{W-6}}│")
    print(f"  ├{'─' * (W - 6)}┤")
    print(f"  │  总请求:    {N_REQS:>6}    成功: {len(lats):>6}    失败: {errs:>4}{'':>{W-52}}│")
    print(f"  │  墙钟时间:  {wall:>7.1f}s{'':>{W-22}}│")
    print(f"  │  总 comp_tokens (含 thinking): {total_comp_tok:>8}{'':>{W-44}}│")
    print(f"  │{'':>{W-6}}│")

    tok_s = total_comp_tok / wall if wall > 0 else 0
    req_s = len(lats) / wall if wall > 0 else 0
    print(f"  │  ★ 吞吐:   {tok_s:>8.1f} tok/s   ({req_s:.2f} req/s){'':>{W-51}}│")

    if lats_sorted:
        avg_lat = statistics.mean(lats)
        med_lat = statistics.median(lats)
        p90 = percentile(lats_sorted, 0.90)
        p95 = percentile(lats_sorted, 0.95)
        p99 = percentile(lats_sorted, 0.99)
        min_lat = lats_sorted[0]
        max_lat = lats_sorted[-1]

        print(f"  │{'':>{W-6}}│")
        print(f"  │  延迟分布:{'':>{W-14}}│")
        print(f"  │    avg  = {avg_lat:>6.2f}s{'':>{W-22}}│")
        print(f"  │    med  = {med_lat:>6.2f}s{'':>{W-22}}│")
        print(f"  │    P90  = {p90:>6.2f}s{'':>{W-22}}│")
        print(f"  │    P95  = {p95:>6.2f}s{'':>{W-22}}│")
        print(f"  │    P99  = {p99:>6.2f}s{'':>{W-22}}│")
        print(f"  │    min  = {min_lat:>6.2f}s{'':>{W-22}}│")
        print(f"  │    max  = {max_lat:>6.2f}s{'':>{W-22}}│")

    if comp_tokens_list:
        avg_tok = statistics.mean(comp_tokens_list)
        print(f"  │{'':>{W-6}}│")
        print(f"  │  平均 completion_tokens/req: {avg_tok:>6.1f} (含 thinking){'':>{W-54}}│")

    print(f"  └{'─' * (W - 6)}┘")

    return {
        "wall": wall, "tok_s": tok_s, "req_s": req_s,
        "total_comp_tok": total_comp_tok,
        "avg_lat": statistics.mean(lats) if lats else 0,
        "p50": percentile(lats_sorted, 0.50) if lats_sorted else 0,
        "p90": percentile(lats_sorted, 0.90) if lats_sorted else 0,
        "p99": percentile(lats_sorted, 0.99) if lats_sorted else 0,
        "ok": len(lats), "err": errs,
    }


# ═══════════════════════════════════════════════════════════════
#  Phase 4: 关闭 thinking 的对比 (可选)
# ═══════════════════════════════════════════════════════════════
def phase4_no_thinking_comparison():
    """关闭 thinking 后的 128 并发对比"""
    print(f"\n{'=' * W}")
    print("  Phase 4: 关闭 Thinking 对比 (128并发, max_tokens=512)")
    print(f"{'=' * W}")

    CONC = 128
    N_REQS = 256

    def do_request_no_think(prompt, max_tokens=512):
        body = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.6,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode()
        req = Request(URL, data=body, headers={"Content-Type": "application/json"})

        t0 = time.perf_counter()
        try:
            resp = urlopen(req, timeout=TIMEOUT)
            data = json.loads(resp.read())
            latency = time.perf_counter() - t0
            ch = data["choices"][0]
            usage = data.get("usage", {})
            return {
                "ok": True,
                "latency": latency,
                "completion_tokens": usage.get("completion_tokens", 0),
                "finish_reason": ch.get("finish_reason", "?"),
                "error": None,
            }
        except Exception as e:
            return {
                "ok": False,
                "latency": time.perf_counter() - t0,
                "completion_tokens": 0,
                "finish_reason": "error",
                "error": str(e)[:120],
            }

    # 先验证 no-think 是否生效
    print("  验证 thinking=off ...")
    r = do_request_no_think("1+1等于几？", 128)
    if r["ok"]:
        print(f"  ✅ thinking=off 有效: {r['latency']:.2f}s, {r['completion_tokens']} tok")
    else:
        print(f"  ❌ thinking=off 失败: {r['error']}")
        print("  跳过 Phase 4")
        return None

    task_prompts = (PROMPTS * ((N_REQS // len(PROMPTS)) + 1))[:N_REQS]

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONC) as pool:
        futures = [pool.submit(do_request_no_think, p, 512) for p in task_prompts]
        results = [f.result() for f in futures]
    wall = time.perf_counter() - t0

    ok_results = [r for r in results if r["ok"]]
    errs = len(results) - len(ok_results)
    total_tok = sum(r["completion_tokens"] for r in ok_results)
    tok_s = total_tok / wall if wall > 0 else 0
    req_s = len(ok_results) / wall if wall > 0 else 0

    lats = sorted([r["latency"] for r in ok_results])
    avg_lat = statistics.mean(lats) if lats else 0
    p50 = percentile(lats, 0.50)
    p99 = percentile(lats, 0.99)
    avg_tok = statistics.mean([r["completion_tokens"] for r in ok_results]) if ok_results else 0

    print(f"\n  thinking=OFF 结果 (C=128, N={N_REQS}):")
    print(f"    吞吐:   {tok_s:.1f} tok/s | {req_s:.2f} req/s")
    print(f"    延迟:   avg={avg_lat:.2f}s  P50={p50:.2f}s  P99={p99:.2f}s")
    print(f"    平均tok: {avg_tok:.1f}  err={errs}")

    return {"tok_s": tok_s, "req_s": req_s, "avg_lat": avg_lat, "p50": p50, "p99": p99}


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════
def main():
    print("╔" + "═" * (W - 2) + "╗")
    print("║" + "SGLang GLM-5  128并发吞吐压测".center(W - 2) + "║")
    print("║" + f"  节点: {URL}".center(W - 2) + "║")
    print("║" + "  thinking=ON (默认) | completion_tokens 含 thinking + output".center(W - 2) + "║")
    print("╚" + "═" * (W - 2) + "╝")

    t_global = time.perf_counter()

    # Phase 0
    if not phase0_warmup():
        print("\n❌ 预热失败，节点不可用，退出")
        sys.exit(1)

    # Phase 1: 基线
    phase1_baseline()

    # Phase 2: 并发梯度
    ladder = phase2_concurrency_ladder()

    # Phase 3: 128 持续压测
    result_128 = phase3_sustained_128()

    # Phase 4: 关闭 thinking 对比
    result_nothink = phase4_no_thinking_comparison()

    # ═══════ 最终汇总 ═══════
    total_time = time.perf_counter() - t_global
    print(f"\n{'═' * W}")
    print("  📊 最终汇总")
    print(f"{'═' * W}")

    if ladder:
        print(f"\n  并发梯度 tok/s (thinking=ON):")
        max_tps = max((v["tok_s"] for v in ladder.values() if v), default=1)
        for conc, data in ladder.items():
            if data:
                bar_len = int(data["tok_s"] / max_tps * 40) if max_tps > 0 else 0
                print(f"    C={conc:>3}: {'█' * bar_len}{'░' * (40 - bar_len)} {data['tok_s']:>8.1f} tok/s")

    if result_128:
        print(f"\n  ★ 128 并发稳态吞吐 (thinking=ON):")
        print(f"    {result_128['tok_s']:.1f} tok/s  |  {result_128['req_s']:.2f} req/s")
        print(f"    延迟: avg={result_128['avg_lat']:.2f}s  P50={result_128['p50']:.2f}s  "
              f"P90={result_128['p90']:.2f}s  P99={result_128['p99']:.2f}s")

    if result_nothink:
        print(f"\n  ★ 128 并发稳态吞吐 (thinking=OFF):")
        print(f"    {result_nothink['tok_s']:.1f} tok/s  |  {result_nothink['req_s']:.2f} req/s")
        print(f"    延迟: avg={result_nothink['avg_lat']:.2f}s  P50={result_nothink['p50']:.2f}s  "
              f"P99={result_nothink['p99']:.2f}s")

    if result_128 and result_nothink and result_nothink["tok_s"] > 0:
        ratio = result_128["tok_s"] / result_nothink["tok_s"]
        print(f"\n  thinking ON/OFF tok/s 比值: {ratio:.2f}x")

    print(f"\n  总测试耗时: {total_time:.1f}s")
    print(f"{'═' * W}\n")


if __name__ == "__main__":
    main()
