#!/usr/bin/env python3
"""
Model backend comparison benchmark: vLLM vs SGLang
Both nodes with thinking disabled, pure generation mode.

Usage:
    python3 debug/bench_quick.py              # full test
    python3 debug/bench_quick.py --quick      # quick mode (halved)
    python3 debug/bench_quick.py --phase 1    # single phase only
"""

import os, requests, time, json, sys, statistics, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# ═══════════════════════════════════════════════════════════════
#  节点配置
# ═══════════════════════════════════════════════════════════════
NODES = {
    "vLLM": {
        "url":  "http://33.235.203.59:8080/v1/chat/completions",
        "model":"glm5",
        "extra": {"chat_template_kwargs": {"enable_thinking": False}},
    },
    "SGLang": {
        "url": "http://33.235.216.5:8081/v1/chat/completions",
        "model": os.environ.get("SGLANG_MODEL", "gpt-4o"),
        "extra": {},
    },
}

# ═══════════════════════════════════════════════════════════════
#  测试 Prompt 集
# ═══════════════════════════════════════════════════════════════
SHORT_PROMPTS = [
    "2+3等于几？",
    "中国的首都是哪里？",
    "用一句话解释什么是机器学习",
    "hello翻译成中文",
    "世界上最大的海洋是什么？",
    "Python和Java的主要区别是什么？",
]

LONG_PROMPTS = [
    "请详细解释量子计算的基本原理，包括量子比特、叠加态和纠缠的概念",
    "写一篇300字左右的短文，分析人工智能对教育行业的影响",
    "详细比较TCP和UDP协议的区别，给出各自适用的场景",
]

W = 78  # 输出宽度


# ═══════════════════════════════════════════════════════════════
#  核心请求函数
# ═══════════════════════════════════════════════════════════════
def do_request(url, model, prompt, max_tokens, extra, timeout=120):
    """同步请求，返回 (latency, tokens, content, finish_reason, error)"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.6,
        **extra,
    }
    t0 = time.perf_counter()
    try:
        r = requests.post(url, json=body, timeout=timeout)
        lat = time.perf_counter() - t0
        d = r.json()
        if "error" in d:
            return lat, 0, d["error"].get("message", str(d["error"])), "error", None
        ch = d["choices"][0]
        msg = ch["message"]
        content = msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""
        toks = d.get("usage", {}).get("completion_tokens", 0)
        return lat, toks, content, ch.get("finish_reason", "?"), None
    except Exception as e:
        return time.perf_counter() - t0, 0, "", "error", str(e)


def do_stream(url, model, prompt, max_tokens, extra, timeout=120):
    """流式请求，返回 (ttft, total_time, total_chunks, total_chars)"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.6,
        "stream": True,
        **extra,
    }
    t0 = time.perf_counter()
    ttft = None
    chunks = 0
    chars = 0
    try:
        r = requests.post(url, json=body, timeout=timeout, stream=True)
        for line in r.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8", "replace")
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
                delta = d["choices"][0].get("delta", {})
                txt = delta.get("content") or delta.get("reasoning_content") or ""
                if txt:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    chunks += 1
                    chars += len(txt)
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        total = time.perf_counter() - t0
        return ttft or total, total, chunks, chars
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return elapsed, elapsed, 0, 0


# ═══════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════
def make_req_fn(cfg, prompt, max_tokens):
    """创建一个无参闭包，方便丢到线程池"""
    def fn():
        return do_request(cfg["url"], cfg["model"], prompt, max_tokens, cfg["extra"])
    return fn


def fmt_bar(label, value, max_val, width=30):
    """简单 ASCII 柱状图"""
    if max_val <= 0:
        filled = 0
    else:
        filled = int(value / max_val * width)
    return f"  {label:>6s} |{'█' * filled}{'░' * (width - filled)}| {value:.1f}"


def percentile(data, pct):
    """计算百分位数"""
    if not data:
        return 0
    s = sorted(data)
    idx = int(len(s) * pct / 100)
    return s[min(idx, len(s) - 1)]


def print_header(title):
    print(f"\n{'=' * W}")
    print(f"  {title}")
    print(f"{'=' * W}")


# ═══════════════════════════════════════════════════════════════
#  Phase 0: 连通性 & 预热
# ═══════════════════════════════════════════════════════════════
def phase0_warmup():
    print_header("Phase 0: 连通性检查 & 预热")
    for name, cfg in NODES.items():
        lat, toks, content, reason, err = do_request(
            cfg["url"], cfg["model"], "hi", 8, cfg["extra"], timeout=30
        )
        status = "✅" if reason != "error" else "❌"
        preview = content[:60].replace("\n", " ") if content else (err or "empty")
        print(f"  {status} {name:>6s} | {lat:.2f}s | {toks}tok | {reason} | {preview}")
        if reason == "error":
            print(f"    ⚠️  {name} 不可用，后续测试可能失败")
    print()


# ═══════════════════════════════════════════════════════════════
#  Phase 1: 单请求延迟 (短输出)
# ═══════════════════════════════════════════════════════════════
def phase1_short_latency(prompts):
    print_header("Phase 1: 单请求延迟 — 短输出 (max_tokens=64)")
    results = {name: [] for name in NODES}

    for i, p in enumerate(prompts, 1):
        print(f"\n  [{i}/{len(prompts)}] Q: {p}")
        for name, cfg in NODES.items():
            lat, toks, content, reason, err = do_request(
                cfg["url"], cfg["model"], p, 64, cfg["extra"]
            )
            spd = toks / lat if lat > 0 else 0
            preview = content[:55].replace("\n", " ")
            print(f"    {name:>6s}: {lat:5.2f}s | {toks:3d}tok | {spd:5.1f}tok/s | {preview}")
            if reason != "error":
                results[name].append({"lat": lat, "toks": toks, "spd": spd})

    # 汇总
    print(f"\n  {'─' * 60}")
    print(f"  {'汇总':>6s}   {'平均延迟':>8s}  {'平均tok/s':>10s}  {'平均tok数':>10s}")
    print(f"  {'─' * 60}")
    for name, rs in results.items():
        if rs:
            avg_lat = statistics.mean([r["lat"] for r in rs])
            avg_spd = statistics.mean([r["spd"] for r in rs])
            avg_tok = statistics.mean([r["toks"] for r in rs])
            print(f"  {name:>6s}   {avg_lat:7.2f}s  {avg_spd:9.1f}  {avg_tok:9.1f}")
    return results


# ═══════════════════════════════════════════════════════════════
#  Phase 2: 单请求延迟 (长输出)
# ═══════════════════════════════════════════════════════════════
def phase2_long_latency(prompts):
    print_header("Phase 2: 单请求延迟 — 长输出 (max_tokens=512)")
    results = {name: [] for name in NODES}

    for i, p in enumerate(prompts, 1):
        print(f"\n  [{i}/{len(prompts)}] Q: {p[:40]}...")
        for name, cfg in NODES.items():
            lat, toks, content, reason, err = do_request(
                cfg["url"], cfg["model"], p, 512, cfg["extra"]
            )
            spd = toks / lat if lat > 0 else 0
            print(f"    {name:>6s}: {lat:6.2f}s | {toks:3d}tok | {spd:5.1f}tok/s | {reason}")
            if reason != "error":
                results[name].append({"lat": lat, "toks": toks, "spd": spd})

    # 汇总
    print(f"\n  {'─' * 60}")
    for name, rs in results.items():
        if rs:
            avg_lat = statistics.mean([r["lat"] for r in rs])
            avg_spd = statistics.mean([r["spd"] for r in rs])
            avg_tok = statistics.mean([r["toks"] for r in rs])
            print(f"  {name:>6s}: avg_lat={avg_lat:.2f}s  avg_spd={avg_spd:.1f}tok/s  avg_tok={avg_tok:.0f}")
    return results


# ═══════════════════════════════════════════════════════════════
#  Phase 3: 流式 TTFT
# ═══════════════════════════════════════════════════════════════
def phase3_streaming(prompts):
    print_header("Phase 3: 流式首 Token 延迟 (TTFT) & 生成速度 (max_tokens=128)")
    results = {name: [] for name in NODES}

    for i, p in enumerate(prompts, 1):
        print(f"\n  [{i}/{len(prompts)}] Q: {p}")
        for name, cfg in NODES.items():
            ttft, total, chunks, chars = do_stream(
                cfg["url"], cfg["model"], p, 128, cfg["extra"]
            )
            gen_time = total - ttft if total > ttft else total
            stream_spd = chunks / gen_time if gen_time > 0 else 0
            print(
                f"    {name:>6s}: TTFT={ttft * 1000:5.0f}ms | "
                f"total={total:5.2f}s | chunks={chunks:3d} | "
                f"stream_spd={stream_spd:.1f}chunk/s"
            )
            results[name].append({"ttft": ttft, "total": total, "chunks": chunks})

    # 汇总
    print(f"\n  {'─' * 60}")
    for name, rs in results.items():
        if rs:
            avg_ttft = statistics.mean([r["ttft"] for r in rs]) * 1000
            p50_ttft = percentile([r["ttft"] for r in rs], 50) * 1000
            p99_ttft = percentile([r["ttft"] for r in rs], 99) * 1000
            print(f"  {name:>6s}: TTFT avg={avg_ttft:.0f}ms  p50={p50_ttft:.0f}ms  p99={p99_ttft:.0f}ms")
    return results


# ═══════════════════════════════════════════════════════════════
#  Phase 4: 并发吞吐
# ═══════════════════════════════════════════════════════════════
def phase4_concurrency(prompts, concurrency_levels):
    print_header("Phase 4: 并发吞吐测试 (max_tokens=128)")
    results = {}

    for name, cfg in NODES.items():
        print(f"\n  ── {name} ──")
        print(f"  {'C':>4s} {'N':>4s} | {'Wall':>6s} | {'tok/s':>8s} {'req/s':>7s} | "
              f"{'AvgLat':>7s} {'P50':>7s} {'P99':>7s} | {'Err':>3s}")
        print(f"  {'─' * 68}")

        node_results = []
        for conc in concurrency_levels:
            n_reqs = max(conc * 2, 4)
            task_prompts = (prompts * ((n_reqs // len(prompts)) + 1))[:n_reqs]

            # 构建任务
            futures_data = []
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=conc) as pool:
                futures = []
                for p in task_prompts:
                    f = pool.submit(
                        do_request, cfg["url"], cfg["model"], p, 128, cfg["extra"]
                    )
                    futures.append(f)
                results_list = [f.result() for f in futures]
            wall = time.perf_counter() - t0

            lats = [r[0] for r in results_list if r[3] != "error"]
            total_toks = sum(r[1] for r in results_list if r[3] != "error")
            errs = sum(1 for r in results_list if r[3] == "error")

            tok_s = total_toks / wall if wall > 0 else 0
            req_s = len(lats) / wall if wall > 0 else 0
            avg_lat = statistics.mean(lats) if lats else 0
            p50 = percentile(lats, 50)
            p99 = percentile(lats, 99)

            row = {
                "conc": conc, "n": n_reqs, "wall": wall,
                "tok_s": tok_s, "req_s": req_s,
                "avg_lat": avg_lat, "p50": p50, "p99": p99, "errs": errs,
            }
            node_results.append(row)

            print(
                f"  {conc:>4d} {n_reqs:>4d} | {wall:5.1f}s | "
                f"{tok_s:7.1f} {req_s:6.2f} | "
                f"{avg_lat:6.2f}s {p50:6.2f}s {p99:6.2f}s | {errs:>3d}"
            )

        results[name] = node_results

    # 对比柱状图
    print(f"\n  ── 吞吐对比 (tok/s) ──")
    max_toks = max(
        r["tok_s"]
        for rows in results.values()
        for r in rows
    ) if results else 1
    for conc in concurrency_levels:
        print(f"  C={conc}:")
        for name in NODES:
            row = next((r for r in results.get(name, []) if r["conc"] == conc), None)
            if row:
                print(fmt_bar(name, row["tok_s"], max_toks, 35) + " tok/s")

    return results


# ═══════════════════════════════════════════════════════════════
#  Phase 5: 压力测试 (高并发持续)
# ═══════════════════════════════════════════════════════════════
def phase5_stress(prompts, conc=16, duration_target=60):
    print_header(f"Phase 5: 压力测试 (C={conc}, 目标~{duration_target}s)")
    # 估算请求数：假设每请求平均3s，那60s大约需要 conc*60/3 个请求
    n_reqs = max(conc * (duration_target // 3), 32)
    task_prompts = (prompts * ((n_reqs // len(prompts)) + 1))[:n_reqs]

    for name, cfg in NODES.items():
        print(f"\n  ── {name}: {n_reqs} requests, C={conc} ──")
        lats = []
        toks_total = 0
        errs = 0
        done = 0

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=conc) as pool:
            futures = [
                pool.submit(do_request, cfg["url"], cfg["model"], p, 128, cfg["extra"])
                for p in task_prompts
            ]
            for f in as_completed(futures):
                lat, toks, content, reason, err = f.result()
                done += 1
                if reason != "error":
                    lats.append(lat)
                    toks_total += toks
                else:
                    errs += 1
                # 进度
                if done % 10 == 0 or done == n_reqs:
                    elapsed = time.perf_counter() - t0
                    print(f"    进度: {done}/{n_reqs} | "
                          f"elapsed={elapsed:.1f}s | "
                          f"tok/s={toks_total / elapsed:.1f} | "
                          f"err={errs}", flush=True)

        wall = time.perf_counter() - t0
        print(f"\n    ┌{'─' * 50}┐")
        print(f"    │ 总请求: {n_reqs:>6d}  成功: {len(lats):>6d}  失败: {errs:>4d}     │")
        print(f"    │ 墙钟:  {wall:>7.1f}s                                │")
        print(f"    │ 吞吐:  {toks_total / wall:>7.1f} tok/s   {len(lats) / wall:>6.2f} req/s     │")
        if lats:
            print(f"    │ 延迟:  avg={statistics.mean(lats):>5.2f}s  "
                  f"p50={percentile(lats, 50):>5.2f}s  "
                  f"p99={percentile(lats, 99):>5.2f}s │")
        print(f"    └{'─' * 50}┘")


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="GLM-5-FP8 vLLM vs SGLang Benchmark")
    parser.add_argument("--quick", action="store_true", help="快速模式，减少请求数")
    parser.add_argument("--phase", type=int, default=0,
                        help="只跑指定阶段 (1-5), 0=全部")
    parser.add_argument("--no-stress", action="store_true", help="跳过 Phase 5 压力测试")
    args = parser.parse_args()

    # 根据模式调整
    if args.quick:
        short_ps = SHORT_PROMPTS[:3]
        long_ps = LONG_PROMPTS[:1]
        stream_ps = SHORT_PROMPTS[:2]
        conc_levels = [1, 4, 8]
    else:
        short_ps = SHORT_PROMPTS
        long_ps = LONG_PROMPTS
        stream_ps = SHORT_PROMPTS[:4]
        conc_levels = [1, 2, 4, 8, 16]

    all_prompts = SHORT_PROMPTS + LONG_PROMPTS

    print("╔" + "═" * (W - 2) + "╗")
    print("║" + "GLM-5-FP8  vLLM vs SGLang 公平对比压测".center(W - 2) + "║")
    print("║" + f"thinking=OFF | {'快速' if args.quick else '完整'}模式".center(W - 2) + "║")
    print("╚" + "═" * (W - 2) + "╝")

    run_all = args.phase == 0
    t_global = time.perf_counter()

    if run_all or args.phase == 0:
        phase0_warmup()

    if run_all or args.phase == 1:
        phase1_short_latency(short_ps)

    if run_all or args.phase == 2:
        phase2_long_latency(long_ps)

    if run_all or args.phase == 3:
        phase3_streaming(stream_ps)

    if run_all or args.phase == 4:
        phase4_concurrency(all_prompts, conc_levels)

    if (run_all or args.phase == 5) and not args.no_stress:
        phase5_stress(all_prompts, conc=16, duration_target=60)

    total = time.perf_counter() - t_global
    print(f"\n{'=' * W}")
    print(f"  全部完成 ✅  总耗时: {total:.1f}s")
    print(f"{'=' * W}")


if __name__ == "__main__":
    main()
