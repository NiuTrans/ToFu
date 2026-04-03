#!/usr/bin/env python3
"""
vLLM vs SGLang  ——  C=128 高并发吞吐 & 完成速度对比
====================================================
两端均为 non-thinking 模式。
vLLM  通过 chat_template_kwargs.enable_thinking=False 关闭思考。
SGLang 本身即 non-thinking。

核心关注:
  1) C=128 下的最大吞吐 (output tok/s)
  2) C=128 下的请求完成速度 (req/s, 延迟分布)

用法:
    python3 debug/bench_c128.py                # 完整测试 (预热→梯度并发→C128压测)
    python3 debug/bench_c128.py --quick        # 只跑 C=128 压测
    python3 debug/bench_c128.py --requests 512 # 自定义 C128 压测请求数
"""

import os, json, time, sys, statistics, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict

# ═══════════════════════════════════════════════════════════════
#  端点配置
# ═══════════════════════════════════════════════════════════════
NODES = OrderedDict([
    ("vLLM", {
        "url":   "http://33.236.231.53:8080/v1/chat/completions",
        "model": "glm5",                    # vLLM 通常接受任意 model 名
        "extra": {
            "chat_template_kwargs": {"enable_thinking": False},
        },
    }),
    ("SGLang", {
        "url":   "http://33.236.198.156:8081/v1/chat/completions",
        "model": "glm5",
        "extra": {},                           # 已是 non-thinking
    }),
])

# ═══════════════════════════════════════════════════════════════
#  Prompt 池 —— 混合短中长，模拟真实负载
# ═══════════════════════════════════════════════════════════════
PROMPTS = [
    # 短 prompt (期望短输出)
    "2+3等于几？",
    "中国的首都是哪里？",
    "hello翻译成中文",
    "世界上最大的海洋是什么？",
    "一年有多少天？",
    "什么是HTTP协议？一句话回答",
    "Python的创始人是谁？",
    "1光年大约是多少公里？",
    # 中 prompt (期望中等输出)
    "用python写一个二分查找函数",
    "解释什么是机器学习，3句话以内",
    "TCP和UDP的主要区别是什么？",
    "写一首关于春天的五言绝句",
    "列举5个常见的排序算法及其时间复杂度",
    "JavaScript中var、let、const的区别",
    # 长 prompt (期望较长输出)
    "请详细解释量子计算的基本原理，包括量子比特、叠加态和纠缠",
    "写一篇200字短文分析人工智能对教育行业的影响",
]

W = 88

# ═══════════════════════════════════════════════════════════════
#  底层请求函数 (使用 urllib，无外部依赖)
# ═══════════════════════════════════════════════════════════════
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

def do_request(url, model, prompt, max_tokens, extra, timeout=1800):
    """
    同步 non-stream 请求。
    返回 dict: {ok, latency, output_tokens, prompt_tokens, content, finish_reason, error}
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.6,
        **extra,
    }).encode()
    req = Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        resp = urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        lat = time.perf_counter() - t0

        if "error" in data:
            return {"ok": False, "latency": lat, "output_tokens": 0,
                    "prompt_tokens": 0, "content": "",
                    "finish_reason": "error",
                    "error": str(data["error"])}

        ch = data["choices"][0]
        msg = ch["message"]
        content = msg.get("content") or msg.get("reasoning_content") or ""
        usage = data.get("usage", {})
        return {
            "ok": True,
            "latency": lat,
            "output_tokens": usage.get("completion_tokens", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "content": content,
            "finish_reason": ch.get("finish_reason", "?"),
            "error": None,
        }
    except Exception as e:
        return {"ok": False, "latency": time.perf_counter() - t0,
                "output_tokens": 0, "prompt_tokens": 0, "content": "",
                "finish_reason": "error", "error": str(e)[:200]}


# ═══════════════════════════════════════════════════════════════
#  统计工具
# ═══════════════════════════════════════════════════════════════
def pct(data, p):
    """p 取 0-100"""
    if not data:
        return 0.0
    s = sorted(data)
    idx = min(int(len(s) * p / 100), len(s) - 1)
    return s[idx]


def fmt_bar(label, value, max_val, width=40):
    filled = int(value / max_val * width) if max_val > 0 else 0
    return f"  {label:>7s} |{'█' * filled}{'░' * (width - filled)}| {value:,.1f}"


def hdr(title):
    print(f"\n{'═' * W}")
    print(f"  {title}")
    print(f"{'═' * W}")


# ═══════════════════════════════════════════════════════════════
#  Phase 0: 连通性预热
# ═══════════════════════════════════════════════════════════════
def phase_warmup():
    hdr("Phase 0 · 连通性检查 & 预热  (各发3请求)")
    for name, cfg in NODES.items():
        ok_cnt = 0
        for i in range(3):
            r = do_request(cfg["url"], cfg["model"], "hi", 8, cfg["extra"], timeout=30)
            if r["ok"]:
                ok_cnt += 1
        status = "✅" if ok_cnt == 3 else ("⚠️" if ok_cnt > 0 else "❌")
        print(f"  {status} {name:>6s}  {ok_cnt}/3 成功  |  {cfg['url']}")
        if ok_cnt == 0:
            print(f"    └─ 端点不可用! 后续测试将失败")
    print()


# ═══════════════════════════════════════════════════════════════
#  Phase 1: 梯度并发 (1→128) —— 看吞吐如何随并发增长
# ═══════════════════════════════════════════════════════════════
def phase_gradient(max_tokens=3200):
    hdr("Phase 1 · 梯度并发吞吐  (max_tokens=128)")
    levels = [1, 4, 8, 16, 32, 64, 128]
    all_results = {}

    for name, cfg in NODES.items():
        print(f"\n  ── {name} ──")
        print(f"  {'C':>4s} {'N':>5s} │ {'Wall':>7s} │ {'tok/s':>9s} {'req/s':>8s} │"
              f" {'AvgLat':>7s} {'P50':>7s} {'P90':>7s} {'P99':>7s} │ {'Err':>4s}")
        print(f"  {'─' * 78}")

        node_rows = []
        for conc in levels:
            # 每个并发级别发 conc*2 个请求 (至少8个)
            n_reqs = max(conc * 2, 8)
            prompts = (PROMPTS * ((n_reqs // len(PROMPTS)) + 1))[:n_reqs]

            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=conc) as pool:
                futs = [pool.submit(do_request, cfg["url"], cfg["model"],
                                    p, max_tokens, cfg["extra"])
                        for p in prompts]
                results = [f.result() for f in futs]
            wall = time.perf_counter() - t0

            lats = sorted([r["latency"] for r in results if r["ok"]])
            total_out_tok = sum(r["output_tokens"] for r in results if r["ok"])
            errs = sum(1 for r in results if not r["ok"])
            ok = len(lats)

            tok_s = total_out_tok / wall if wall > 0 else 0
            req_s = ok / wall if wall > 0 else 0
            avg_lat = statistics.mean(lats) if lats else 0
            p50  = pct(lats, 50)
            p90  = pct(lats, 90)
            p99  = pct(lats, 99)

            row = {"conc": conc, "n": n_reqs, "wall": wall,
                   "tok_s": tok_s, "req_s": req_s, "total_tok": total_out_tok,
                   "avg_lat": avg_lat, "p50": p50, "p90": p90, "p99": p99,
                   "ok": ok, "errs": errs}
            node_rows.append(row)

            print(f"  {conc:>4d} {n_reqs:>5d} │ {wall:>6.1f}s │"
                  f" {tok_s:>8.1f} {req_s:>7.2f} │"
                  f" {avg_lat:>6.2f}s {p50:>6.2f}s {p90:>6.2f}s {p99:>6.2f}s │ {errs:>4d}")

        all_results[name] = node_rows

    # ── 并排对比 ──
    print(f"\n  ── 吞吐对比 (output tok/s) ──")
    max_tps = max(r["tok_s"] for rows in all_results.values() for r in rows) or 1
    for conc in levels:
        print(f"  C={conc:>3d}:")
        for name in NODES:
            row = next((r for r in all_results.get(name, []) if r["conc"] == conc), None)
            if row:
                print(fmt_bar(name, row["tok_s"], max_tps) + " tok/s")

    return all_results


# ═══════════════════════════════════════════════════════════════
#  Phase 2: C=128 大规模压测 —— 核心指标
# ═══════════════════════════════════════════════════════════════
def phase_c128_stress(n_reqs=384, max_tokens=3200):
    CONC = 128
    hdr(f"Phase 2 · C={CONC} 大规模压测  ({n_reqs} reqs, max_tokens={max_tokens})")

    summary = {}
    for name, cfg in NODES.items():
        print(f"\n  ── {name}: {n_reqs} 请求, 并发={CONC} ──")
        prompts = (PROMPTS * ((n_reqs // len(PROMPTS)) + 1))[:n_reqs]

        completed = 0
        ok_count  = 0
        err_count = 0
        total_out_tok = 0
        total_prompt_tok = 0
        lats = []
        first_err = None

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=CONC) as pool:
            futs = [pool.submit(do_request, cfg["url"], cfg["model"],
                                p, max_tokens, cfg["extra"])
                    for p in prompts]

            for f in as_completed(futs):
                r = f.result()
                completed += 1
                if r["ok"]:
                    ok_count += 1
                    lats.append(r["latency"])
                    total_out_tok += r["output_tokens"]
                    total_prompt_tok += r["prompt_tokens"]
                else:
                    err_count += 1
                    if first_err is None:
                        first_err = r["error"]

                # 每50个请求或最后打一次进度
                if completed % 50 == 0 or completed == n_reqs:
                    elapsed = time.perf_counter() - t0
                    cur_tps = total_out_tok / elapsed if elapsed > 0 else 0
                    print(f"    进度 {completed:>4d}/{n_reqs} │"
                          f" elapsed={elapsed:>6.1f}s │"
                          f" ok={ok_count} err={err_count} │"
                          f" 实时tok/s={cur_tps:>7.1f}", flush=True)

        wall = time.perf_counter() - t0

        # ── 详细报告 ──
        tok_s = total_out_tok / wall if wall > 0 else 0
        req_s = ok_count / wall if wall > 0 else 0
        avg_lat = statistics.mean(lats) if lats else 0
        med_lat = statistics.median(lats) if lats else 0
        p90 = pct(lats, 90)
        p95 = pct(lats, 95)
        p99 = pct(lats, 99)
        min_lat = min(lats) if lats else 0
        max_lat = max(lats) if lats else 0
        avg_out_tok = total_out_tok / ok_count if ok_count else 0

        summary[name] = {
            "wall": wall, "tok_s": tok_s, "req_s": req_s,
            "ok": ok_count, "err": err_count,
            "avg_lat": avg_lat, "med_lat": med_lat,
            "p90": p90, "p95": p95, "p99": p99,
            "min_lat": min_lat, "max_lat": max_lat,
            "total_out_tok": total_out_tok,
            "avg_out_tok": avg_out_tok,
        }

        print(f"\n    ┌{'─' * 62}┐")
        print(f"    │  {name} — C=128 压测结果                                     │")
        print(f"    ├{'─' * 62}┤")
        print(f"    │  总请求: {n_reqs:>6d}   成功: {ok_count:>6d}   失败: {err_count:>6d}           │")
        print(f"    │  墙钟时间:     {wall:>8.1f}s                                  │")
        print(f"    │  ─────────────────────────────────────────────────────────── │")
        print(f"    │  🔥 吞吐:      {tok_s:>8.1f} output tok/s                     │")
        print(f"    │  🔥 请求速度:  {req_s:>8.2f} req/s                             │")
        print(f"    │  ─────────────────────────────────────────────────────────── │")
        print(f"    │  延迟分布:                                                   │")
        print(f"    │    avg  = {avg_lat:>7.2f}s    min = {min_lat:>7.2f}s                   │")
        print(f"    │    P50  = {med_lat:>7.2f}s    P90 = {p90:>7.2f}s                   │")
        print(f"    │    P95  = {p95:>7.2f}s    P99 = {p99:>7.2f}s                   │")
        print(f"    │    max  = {max_lat:>7.2f}s                                     │")
        print(f"    │  ─────────────────────────────────────────────────────────── │")
        print(f"    │  平均输出: {avg_out_tok:>6.1f} tok/req                            │")
        print(f"    │  总输出:   {total_out_tok:>8d} tokens                             │")
        print(f"    └{'─' * 62}┘")
        if first_err:
            print(f"    ⚠️  首个错误: {first_err[:100]}")

    return summary


# ═══════════════════════════════════════════════════════════════
#  Phase 3: 最终并排对比
# ═══════════════════════════════════════════════════════════════
def phase_comparison(summary):
    hdr("Phase 3 · C=128 最终对比")

    names = list(NODES.keys())
    if len(names) < 2 or any(n not in summary for n in names):
        print("  ⚠️  缺少某节点数据，跳过对比")
        return

    a, b = names[0], names[1]
    sa, sb = summary[a], summary[b]

    def winner_higher(va, vb):
        if va > vb: return f"← {a}"
        elif vb > va: return f"{b} →"
        return "TIE"

    def winner_lower(va, vb):
        if va < vb: return f"← {a}"
        elif vb < va: return f"{b} →"
        return "TIE"

    def ratio(va, vb):
        if min(va, vb) > 0:
            return f"{max(va,vb)/min(va,vb):.2f}x"
        return "N/A"

    rows = [
        ("🔥 吞吐 (tok/s)",   f"{sa['tok_s']:>9.1f}", f"{sb['tok_s']:>9.1f}",
         winner_higher(sa['tok_s'], sb['tok_s']),  ratio(sa['tok_s'], sb['tok_s'])),
        ("🔥 请求速度 (req/s)", f"{sa['req_s']:>9.2f}", f"{sb['req_s']:>9.2f}",
         winner_higher(sa['req_s'], sb['req_s']),  ratio(sa['req_s'], sb['req_s'])),
        ("平均延迟",           f"{sa['avg_lat']:>8.2f}s", f"{sb['avg_lat']:>8.2f}s",
         winner_lower(sa['avg_lat'], sb['avg_lat']), ratio(sa['avg_lat'], sb['avg_lat'])),
        ("P50 延迟",           f"{sa['med_lat']:>8.2f}s", f"{sb['med_lat']:>8.2f}s",
         winner_lower(sa['med_lat'], sb['med_lat']), ratio(sa['med_lat'], sb['med_lat'])),
        ("P90 延迟",           f"{sa['p90']:>8.2f}s",    f"{sb['p90']:>8.2f}s",
         winner_lower(sa['p90'], sb['p90']),         ratio(sa['p90'], sb['p90'])),
        ("P99 延迟",           f"{sa['p99']:>8.2f}s",    f"{sb['p99']:>8.2f}s",
         winner_lower(sa['p99'], sb['p99']),         ratio(sa['p99'], sb['p99'])),
        ("成功率",             f"{sa['ok']}/{sa['ok']+sa['err']}",
                               f"{sb['ok']}/{sb['ok']+sb['err']}",
         "", ""),
        ("平均输出tok",        f"{sa['avg_out_tok']:>8.1f}", f"{sb['avg_out_tok']:>8.1f}",
         "", ""),
    ]

    print(f"\n  {'指标':<22s}│{a:>12s}│{b:>12s}│{'胜出':>10s}│{'差距':>8s}")
    print(f"  {'─'*22}┼{'─'*12}┼{'─'*12}┼{'─'*10}┼{'─'*8}")
    for label, va, vb, win, gap in rows:
        print(f"  {label:<22s}│{va:>12s}│{vb:>12s}│{win:>10s}│{gap:>8s}")

    # ── 吞吐柱状图 ──
    print(f"\n  ── 吞吐柱状图 (output tok/s @ C=128) ──")
    max_tps = max(sa["tok_s"], sb["tok_s"]) or 1
    for name in names:
        s = summary[name]
        print(fmt_bar(name, s["tok_s"], max_tps, 45) + " tok/s")

    # ── 延迟柱状图 ──
    print(f"\n  ── P50 延迟柱状图 (越短越好) ──")
    max_lat = max(sa["med_lat"], sb["med_lat"]) or 1
    for name in names:
        s = summary[name]
        print(fmt_bar(name, s["med_lat"], max_lat, 45) + " s")


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="vLLM vs SGLang  C=128 高并发吞吐对比")
    parser.add_argument("--quick", action="store_true",
                        help="跳过梯度并发，只跑 C=128 压测")
    parser.add_argument("--requests", type=int, default=384,
                        help="C=128 压测的总请求数 (默认384 = 128×3)")
    parser.add_argument("--max-tokens", type=int, default=128,
                        help="每请求最大输出 tokens (默认128)")
    args = parser.parse_args()

    print("╔" + "═" * (W - 2) + "╗")
    print("║" + " vLLM vs SGLang · C=128 高并发吞吐 & 完成速度对比 ".center(W - 2) + "║")
    print("║" + " thinking=OFF (both) · non-streaming ".center(W - 2) + "║")
    print("╚" + "═" * (W - 2) + "╝")
    print(f"\n  vLLM  : {NODES['vLLM']['url']}")
    print(f"  SGLang: {NODES['SGLang']['url']}")
    print(f"  请求数: {args.requests}   max_tokens: {args.max_tokens}")

    t_global = time.perf_counter()

    # 预热
    phase_warmup()

    # 梯度并发 (除非 --quick)
    if not args.quick:
        phase_gradient(max_tokens=args.max_tokens)

    # C=128 压测 (核心)
    summary = phase_c128_stress(n_reqs=args.requests, max_tokens=args.max_tokens)

    # 最终对比
    phase_comparison(summary)

    total = time.perf_counter() - t_global
    print(f"\n{'═' * W}")
    print(f"  全部完成 ✅  总耗时: {total:.1f}s")
    print(f"{'═' * W}\n")


if __name__ == "__main__":
    main()
