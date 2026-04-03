#!/usr/bin/env python3
"""
vLLM vs SGLang 吞吐对比 — Thinking 模式, 64 并发, 真实 prompt
=============================================================
从 IFEvalG enhanced_messages_v2.jsonl 随机抽 500 条真实问题,
两个节点均开启 thinking 模式, 不限制 max_tokens, 只跑 64 并发吞吐.

Usage:
    python3 debug/bench_thinking_throughput.py
    python3 debug/bench_thinking_throughput.py --data /path/to/data.jsonl
    python3 debug/bench_thinking_throughput.py --n-prompts 200   # 少跑一点
    python3 debug/bench_thinking_throughput.py --concurrency 32  # 改并发数
"""

import os, json, time, sys, random, statistics, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError

# ═══════════════════════════════════════════════════════════════
#  节点配置 — 均开启 thinking
# ═══════════════════════════════════════════════════════════════
NODES = {
    "vLLM": {
        "url":  os.environ.get("VLLM_URL", "http://33.235.242.112:8080/v1/chat/completions"),
        "model": os.environ.get("VLLM_MODEL", "qwen35-fp8"),
        "extra": {"chat_template_kwargs": {"enable_thinking": True}},
    },
    "SGLang": {
        "url":  os.environ.get("SGLANG_URL", "http://33.235.216.5:8081/v1/chat/completions"),
        "model": os.environ.get("SGLANG_MODEL", "glm5"),
        "extra": {"chat_template_kwargs": {"enable_thinking": True}},
    },
}

DEFAULT_DATA = (
    "/path/to/your/data"
    "your-username/open-instruct/open_instruct/IFEvalG/enhanced_messages_v2.jsonl"
)

TIMEOUT = 600  # thinking 模式输出可能很长, 给足超时
W = 90

# ═══════════════════════════════════════════════════════════════
#  数据加载
# ═══════════════════════════════════════════════════════════════
def load_prompts(path: str, n: int = 500, seed: int = 42) -> list[str]:
    """从 JSONL 随机抽取 n 条 user prompt"""
    print(f"  📂 加载数据: {path}")
    all_prompts = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                msgs = d.get("messages", [])
                # 取最后一条 user message 的 content
                for m in reversed(msgs):
                    if m.get("role") == "user" and m.get("content"):
                        all_prompts.append(m["content"])
                        break
            except (json.JSONDecodeError, KeyError):
                continue

    print(f"  📊 总条目: {len(all_prompts)}")
    rng = random.Random(seed)
    sampled = rng.sample(all_prompts, min(n, len(all_prompts)))
    print(f"  🎲 抽样: {len(sampled)} 条 (seed={seed})")

    # 打印 prompt 长度分布
    lens = [len(p) for p in sampled]
    print(f"  📏 prompt 字符长度: min={min(lens)}, median={sorted(lens)[len(lens)//2]}, "
          f"max={max(lens)}, avg={sum(lens)/len(lens):.0f}")
    return sampled


# ═══════════════════════════════════════════════════════════════
#  核心请求函数 (不限 max_tokens)
# ═══════════════════════════════════════════════════════════════
def do_request(url: str, model: str, prompt: str, extra: dict,
               timeout: int = TIMEOUT) -> tuple:
    """
    同步请求, 不设 max_tokens.
    返回 (latency, completion_tokens, prompt_tokens, finish_reason, error)
    """
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.6,
        **extra,
    }
    payload = json.dumps(body).encode()
    req = Request(url, data=payload, headers={"Content-Type": "application/json"})

    t0 = time.perf_counter()
    try:
        resp = urlopen(req, timeout=timeout)
        raw = resp.read()
        lat = time.perf_counter() - t0

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return lat, 0, 0, "error", f"invalid JSON: {raw[:200]}"

        if "error" in data:
            return lat, 0, 0, "error", str(data["error"])[:200]

        usage = data.get("usage") or {}
        comp_toks = usage.get("completion_tokens", 0) or 0
        prompt_toks = usage.get("prompt_tokens", 0) or 0

        choices = data.get("choices")
        if choices and len(choices) > 0:
            reason = (choices[0] or {}).get("finish_reason", "?")
        else:
            # choices 是 None 或空列表, 尝试从顶层取 content
            reason = "no_choices"
            # 仍然尝试计 tokens: 有些引擎不返回 choices 但有 usage
            if comp_toks == 0:
                # 把原始响应前 300 字符作为 error 方便调试
                return lat, 0, prompt_toks, "error", f"no choices in resp: {json.dumps(data, ensure_ascii=False)[:300]}"

        return lat, comp_toks, prompt_toks, reason, None

    except Exception as e:
        return time.perf_counter() - t0, 0, 0, "error", str(e)[:300]


# ═══════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════
def percentile(data, pct):
    if not data:
        return 0
    s = sorted(data)
    idx = int(len(s) * pct / 100)
    return s[min(idx, len(s) - 1)]


def fmt_duration(s):
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(s, 60)
    return f"{int(m)}m{sec:.0f}s"


# ═══════════════════════════════════════════════════════════════
#  Phase 0: 连通性 & 预热
# ═══════════════════════════════════════════════════════════════
def phase_warmup():
    print(f"\n{'=' * W}")
    print("  Phase 0: 连通性检查 & 预热 (thinking=ON)")
    print(f"{'=' * W}")

    alive_nodes = []
    for name, cfg in NODES.items():
        body = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": "1+1=?"}],
            "max_tokens": 32,
            "temperature": 0.6,
            **cfg["extra"],
        }
        payload = json.dumps(body).encode()
        req = Request(cfg["url"], data=payload,
                      headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            resp = urlopen(req, timeout=30)
            raw = resp.read()
            data = json.loads(raw)
            lat = time.perf_counter() - t0

            if "error" in data:
                print(f"  ❌ {name:>6s} | API error: {str(data['error'])[:200]}")
                continue

            usage = data.get("usage") or {}
            comp = usage.get("completion_tokens", 0) or 0
            choices = data.get("choices") or []
            if choices:
                c0 = choices[0] or {}
                reason = c0.get("finish_reason", "?")
                msg = c0.get("message") or {}
                content = msg.get("content", "") or ""
                thinking = msg.get("reasoning_content", "") or ""
            else:
                reason = "no_choices"
                content = ""
                thinking = ""
                print(f"  ⚠️ {name:>6s} | 连通但无 choices: {json.dumps(data, ensure_ascii=False)[:200]}")

            has_think = "✅" if thinking else "⚠️ no thinking"
            preview = content[:60].replace("\n", " ")
            print(f"  ✅ {name:>6s} | {lat:.2f}s | {comp}tok | {reason} | think={has_think}")
            print(f"           preview: {preview}")
            alive_nodes.append(name)
        except Exception as e:
            print(f"  ❌ {name:>6s} | error: {e}")

    if not alive_nodes:
        print(f"\n  🚫 所有节点均不可达, 中止测试!")
        sys.exit(1)

    return alive_nodes


# ═══════════════════════════════════════════════════════════════
#  Phase 1: 64 并发吞吐测试
# ═══════════════════════════════════════════════════════════════
def phase_throughput(prompts: list[str], concurrency: int = 64, alive_nodes: list[str] | None = None):
    n_reqs = len(prompts)
    print(f"\n{'=' * W}")
    print(f"  Phase 1: 并发吞吐测试")
    print(f"  thinking=ON | concurrency={concurrency} | requests={n_reqs} | max_tokens=无限制")
    print(f"{'=' * W}")

    results = {}

    for name, cfg in NODES.items():
        if alive_nodes and name not in alive_nodes:
            print(f"\n  ── {name} SKIPPED (连通性检查失败) ──")
            continue
        print(f"\n  ── {name} ({cfg['url']}) ──")

        lats = []
        comp_tokens_list = []
        prompt_tokens_list = []
        errors = 0
        done = 0
        total_comp_toks = 0

        t_wall_start = time.perf_counter()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(
                    do_request, cfg["url"], cfg["model"], p, cfg["extra"]
                ): i
                for i, p in enumerate(prompts)
            }

            for f in as_completed(futures):
                lat, comp_toks, prompt_toks, reason, err = f.result()
                done += 1

                if reason != "error":
                    lats.append(lat)
                    comp_tokens_list.append(comp_toks)
                    prompt_tokens_list.append(prompt_toks)
                    total_comp_toks += comp_toks
                else:
                    errors += 1

                # 进度打印
                if done % 50 == 0 or done == n_reqs:
                    elapsed = time.perf_counter() - t_wall_start
                    cur_tps = total_comp_toks / elapsed if elapsed > 0 else 0
                    print(f"    [{done:>4d}/{n_reqs}] "
                          f"elapsed={fmt_duration(elapsed)} | "
                          f"tok/s={cur_tps:>7.1f} | "
                          f"ok={done - errors} err={errors}",
                          flush=True)

        wall = time.perf_counter() - t_wall_start
        ok = len(lats)
        total_prompt_toks = sum(prompt_tokens_list)

        # 计算指标
        tok_s = total_comp_toks / wall if wall > 0 else 0
        req_s = ok / wall if wall > 0 else 0
        avg_lat = statistics.mean(lats) if lats else 0
        med_lat = statistics.median(lats) if lats else 0
        p50_lat = percentile(lats, 50)
        p90_lat = percentile(lats, 90)
        p99_lat = percentile(lats, 99)
        avg_comp = statistics.mean(comp_tokens_list) if comp_tokens_list else 0
        med_comp = statistics.median(comp_tokens_list) if comp_tokens_list else 0
        max_comp = max(comp_tokens_list) if comp_tokens_list else 0
        min_comp = min(comp_tokens_list) if comp_tokens_list else 0

        r = {
            "name": name, "wall": wall, "ok": ok, "errors": errors,
            "total_comp_toks": total_comp_toks, "total_prompt_toks": total_prompt_toks,
            "tok_s": tok_s, "req_s": req_s,
            "avg_lat": avg_lat, "med_lat": med_lat,
            "p50_lat": p50_lat, "p90_lat": p90_lat, "p99_lat": p99_lat,
            "avg_comp": avg_comp, "med_comp": med_comp,
            "max_comp": max_comp, "min_comp": min_comp,
            "lats": lats, "comp_tokens_list": comp_tokens_list,
        }
        results[name] = r

        # 打印详细结果
        print(f"\n    ┌{'─' * 62}┐")
        print(f"    │  {name} 结果汇总{' ' * (62 - len(name) - 14)}│")
        print(f"    ├{'─' * 62}┤")
        print(f"    │  请求总数: {n_reqs:>6d}    成功: {ok:>6d}    失败: {errors:>4d}       │")
        print(f"    │  墙钟时间: {fmt_duration(wall):>10s}{' ' * 44}│")
        print(f"    │  ─────────────────────────────────────────────────────────── │")
        print(f"    │  🔥 吞吐:   {tok_s:>8.1f} tok/s    {req_s:>6.2f} req/s                │")
        print(f"    │  ─────────────────────────────────────────────────────────── │")
        print(f"    │  延迟 (per-request):                                        │")
        print(f"    │    avg={avg_lat:>7.2f}s  med={med_lat:>7.2f}s                          │")
        print(f"    │    P50={p50_lat:>7.2f}s  P90={p90_lat:>7.2f}s  P99={p99_lat:>7.2f}s       │")
        print(f"    │  ─────────────────────────────────────────────────────────── │")
        print(f"    │  输出 tokens (per-request):                                 │")
        print(f"    │    avg={avg_comp:>7.0f}   med={med_comp:>7.0f}                          │")
        print(f"    │    min={min_comp:>7d}   max={max_comp:>7d}                          │")
        print(f"    │  总输出 tokens: {total_comp_toks:>10d}                              │")
        print(f"    │  总输入 tokens: {total_prompt_toks:>10d}                              │")
        print(f"    └{'─' * 62}┘")

    return results


# ═══════════════════════════════════════════════════════════════
#  对比汇总
# ═══════════════════════════════════════════════════════════════
def print_comparison(results: dict):
    names = list(results.keys())
    if len(names) < 2:
        return

    print(f"\n{'=' * W}")
    print("  📊 vLLM vs SGLang 对比汇总 (Thinking ON, 无 max_tokens 限制)")
    print(f"{'=' * W}")

    a, b = results[names[0]], results[names[1]]

    rows = [
        ("墙钟时间",       f"{fmt_duration(a['wall'])}",       f"{fmt_duration(b['wall'])}"),
        ("成功请求",       f"{a['ok']}",                       f"{b['ok']}"),
        ("失败请求",       f"{a['errors']}",                   f"{b['errors']}"),
        ("",              "",                                  ""),
        ("🔥 吞吐 tok/s", f"{a['tok_s']:.1f}",               f"{b['tok_s']:.1f}"),
        ("   吞吐 req/s", f"{a['req_s']:.2f}",               f"{b['req_s']:.2f}"),
        ("",              "",                                  ""),
        ("延迟 avg",      f"{a['avg_lat']:.2f}s",             f"{b['avg_lat']:.2f}s"),
        ("延迟 P50",      f"{a['p50_lat']:.2f}s",             f"{b['p50_lat']:.2f}s"),
        ("延迟 P90",      f"{a['p90_lat']:.2f}s",             f"{b['p90_lat']:.2f}s"),
        ("延迟 P99",      f"{a['p99_lat']:.2f}s",             f"{b['p99_lat']:.2f}s"),
        ("",              "",                                  ""),
        ("输出tok avg",   f"{a['avg_comp']:.0f}",             f"{b['avg_comp']:.0f}"),
        ("输出tok med",   f"{a['med_comp']:.0f}",             f"{b['med_comp']:.0f}"),
        ("总输出tok",      f"{a['total_comp_toks']}",         f"{b['total_comp_toks']}"),
    ]

    print(f"\n  {'指标':<16} │ {names[0]:>20} │ {names[1]:>20} │ {'胜出':>10}")
    print(f"  {'─' * 16}─┼─{'─' * 20}─┼─{'─' * 20}─┼─{'─' * 10}")

    for label, v1, v2 in rows:
        if not label:
            print(f"  {'─' * 16}─┼─{'─' * 20}─┼─{'─' * 20}─┼─{'─' * 10}")
            continue

        # 判断胜出
        winner = ""
        if "tok/s" in label:
            try:
                winner = names[0] if float(v1) > float(v2) else names[1]
            except ValueError:
                pass
        elif "req/s" in label:
            try:
                winner = names[0] if float(v1) > float(v2) else names[1]
            except ValueError:
                pass
        elif "延迟" in label:
            try:
                f1 = float(v1.rstrip("s"))
                f2 = float(v2.rstrip("s"))
                winner = names[0] if f1 < f2 else names[1]
            except ValueError:
                pass
        elif "墙钟" in label:
            try:
                winner = names[0] if a["wall"] < b["wall"] else names[1]
            except (TypeError, ValueError):
                pass

        wmark = f"← {winner}" if winner else ""
        print(f"  {label:<16} │ {v1:>20} │ {v2:>20} │ {wmark:>10}")

    # 吞吐比
    if a["tok_s"] > 0 and b["tok_s"] > 0:
        ratio = max(a["tok_s"], b["tok_s"]) / min(a["tok_s"], b["tok_s"])
        faster = names[0] if a["tok_s"] > b["tok_s"] else names[1]
        print(f"\n  ⚡ {faster} 吞吐领先 {ratio:.2f}x")

    # ASCII bar
    max_tps = max(a["tok_s"], b["tok_s"])
    bar_w = 40
    print(f"\n  吞吐 tok/s 对比:")
    for name in names:
        r = results[name]
        filled = int(r["tok_s"] / max_tps * bar_w) if max_tps > 0 else 0
        print(f"    {name:>6} |{'█' * filled}{'░' * (bar_w - filled)}| {r['tok_s']:.1f} tok/s")


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="vLLM vs SGLang 吞吐对比 — Thinking ON, 真实 prompt, 无 max_tokens")
    parser.add_argument("--data", default=DEFAULT_DATA,
                        help="JSONL 数据路径")
    parser.add_argument("--n-prompts", type=int, default=500,
                        help="抽样 prompt 数 (default: 500)")
    parser.add_argument("--concurrency", type=int, default=64,
                        help="并发数 (default: 64)")
    parser.add_argument("--seed", type=int, default=42,
                        help="随机种子")
    parser.add_argument("--timeout", type=int, default=600,
                        help="每请求超时秒数 (default: 600)")
    parser.add_argument("--skip-warmup", action="store_true",
                        help="跳过预热阶段")
    args = parser.parse_args()

    global TIMEOUT
    TIMEOUT = args.timeout

    print("╔" + "═" * (W - 2) + "╗")
    print("║" + " vLLM vs SGLang 吞吐压测 — Thinking ON ".center(W - 2) + "║")
    print("║" + f" C={args.concurrency} | N={args.n_prompts} | max_tokens=∞ ".center(W - 2) + "║")
    print("╚" + "═" * (W - 2) + "╝")

    # 加载数据
    prompts = load_prompts(args.data, n=args.n_prompts, seed=args.seed)
    if not prompts:
        print("❌ 没有加载到任何 prompt, 退出")
        sys.exit(1)

    t_global = time.perf_counter()

    # Phase 0: 预热 & 连通性检查
    alive_nodes = None
    if not args.skip_warmup:
        alive_nodes = phase_warmup()

    # Phase 1: 吞吐测试 (只跑连通的节点)
    results = phase_throughput(prompts, concurrency=args.concurrency, alive_nodes=alive_nodes)

    # 对比汇总
    print_comparison(results)

    total = time.perf_counter() - t_global
    print(f"\n{'=' * W}")
    print(f"  全部完成 ✅  总耗时: {fmt_duration(total)}")
    print(f"{'=' * W}")

    # 保存原始结果到 JSON
    out_path = f"debug/bench_thinking_result_{int(time.time())}.json"
    save_data = {}
    for name, r in results.items():
        save_data[name] = {
            k: v for k, v in r.items()
            if k not in ("lats", "comp_tokens_list")  # 太大了不存
        }
        save_data[name]["lat_percentiles"] = {
            "p50": r["p50_lat"], "p90": r["p90_lat"], "p99": r["p99_lat"],
        }
        save_data[name]["config"] = {
            "url": NODES[name]["url"],
            "model": NODES[name]["model"],
            "thinking": True,
            "concurrency": args.concurrency,
            "n_prompts": len(prompts),
        }
    try:
        with open(out_path, "w") as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False, default=str)
        print(f"  💾 结果已保存: {out_path}")
    except Exception as e:
        print(f"  ⚠️ 保存失败: {e}")


if __name__ == "__main__":
    main()
