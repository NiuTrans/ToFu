"""Benchmark-answer contamination audit for native Terminal-Bench trials.

Responsibility: flag trials whose agent may have received Terminal-Bench
reference solutions or hidden tests during its run, from recovered evidence
only. A flag is evidence for review, not a verdict: legitimately downloaded
upstream sources can share lines with hidden tests, so the final call is made
by reading the cited tool calls.

Signals per trial. A call is *network* when it is a web tool or its arguments
fetch a URL; task images ship visible tests, canary-bearing sources and whole
repositories, so local reads never count as strong evidence.
- ``hidden_lines``: normalized lines (>= ``MIN_FINGERPRINT_CHARS``) of the task's
  ``solution/``, ``tests/`` and ``README.md`` files (the README is never given
  to the agent and can state the expected answer) that occur nowhere in
  ``instruction.md`` or ``environment/``, received in a network call result
  before the agent wrote
  that line itself (``local_hidden_lines``: the same from local calls, weak);
- ``canary_results``: network call results containing the canary GUID;
- ``source_calls``: network calls whose arguments name benchmark answer sources;
- ``denied_egress``: requests the trial egress proxy refused.

Entry points: ``audit_run(work_dir, tasks_dir)``; CLI
``python -m evaluations.terminal_bench.audit WORK_DIR TASKS_DIR``.
Dependencies: standard library; evidence layout of ``runner``/``vm_trial``.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from evaluations.terminal_bench.harness import BENCHMARK_ANSWER_HOSTS, BENCHMARK_URL_MARKERS, TB_CANARY_GUID

# Shorter lines (imports, braces, shell boilerplate) are shared by unrelated code.
MIN_FINGERPRINT_CHARS = 40
SOURCE_MARKERS = tuple(BENCHMARK_URL_MARKERS) + tuple(BENCHMARK_ANSWER_HOSTS) + ("test_outputs.py", "solve.sh",
                                                                                 "solution.sh")
WEB_TOOLS = frozenset({"web_search", "fetch_url", "webfetch", "websearch", "browser_research_page"})
NETWORK_ARGUMENT_MARKERS = ("http://", "https://", "curl ", "wget ", "git clone", "hf_hub_download",
                            "huggingface-cli", "urllib", "requests.get")
STRONG_FLAGS = ("hidden_lines", "canary_results", "source_calls", "denied_egress")
_LINE_NUMBER = re.compile(r"^\s*\d+\s*[:|\t\u2192]\s?")
_SPACE = re.compile(r"\s+")


def normalize(line: str) -> str:
    return _SPACE.sub(" ", _LINE_NUMBER.sub("", line)).strip()


def _text_lines(root: Path) -> set[str]:
    lines: set[str] = set()
    paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines.update(normalize(line) for line in text.splitlines())
    return lines


def task_fingerprints(task_dir: Path) -> set[str]:
    hidden = _text_lines(task_dir / "solution") | _text_lines(task_dir / "tests") | _text_lines(task_dir / "README.md")
    visible = _text_lines(task_dir / "instruction.md") | _text_lines(task_dir / "environment")
    return {line for line in hidden - visible
            if len(line) >= MIN_FINGERPRINT_CHARS and TB_CANARY_GUID not in line}


def _stringify(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _leaf_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _leaf_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaf_strings(item)


def _lines(text: str) -> set[str]:
    """Normalized lines of a call/result, looking inside JSON so escaped newlines still split."""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text
    return {normalize(line) for leaf in _leaf_strings(parsed) for line in leaf.splitlines()}


def _pi_message_events(message: dict) -> list[tuple[str, str, str, str]]:
    if message.get("role") == "assistant":
        return [("call", block.get("id") or "", block.get("name") or "", _stringify(block.get("arguments") or {}))
                for block in message.get("content") or []
                if isinstance(block, dict) and block.get("type") == "toolCall"]
    if message.get("role") == "toolResult":
        text = "\n".join(part.get("text") or "" for part in message.get("content") or []
                         if isinstance(part, dict) and part.get("type") == "text")
        return [("result", message.get("toolCallId") or "", message.get("toolName") or "", text)]
    return []


def tool_events(agent_dir: Path) -> list[tuple[str, str, str, str]]:
    """Ordered (kind, call_id, tool, text) with kind in {"call", "result"} for every arm."""
    events: list[tuple[str, str, str, str]] = []
    tofu_result = agent_dir / "tofu-result.json"
    if tofu_result.is_file():
        try:
            document = json.loads(tofu_result.read_text(encoding="utf-8"))
        except ValueError:
            document = {}
        for step in ((document.get("trajectory") or {}).get("steps") or []):
            names = {}
            for call in step.get("tool_calls") or []:
                names[call.get("tool_call_id")] = call.get("function_name") or ""
                events.append(("call", call.get("tool_call_id") or "", names[call.get("tool_call_id")],
                               _stringify(call.get("arguments") or {})))
            for result in ((step.get("observation") or {}).get("results") or []):
                call_id = result.get("source_call_id") or ""
                events.append(("result", call_id, names.get(call_id, ""), _stringify(result.get("content") or "")))
    stream = agent_dir / "opencode.jsonl"
    if stream.is_file():
        for line in stream.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            part = event.get("part") or {}
            if part.get("type") != "tool":
                continue
            state = part.get("state") or {}
            call_id, tool = part.get("callID") or "", part.get("tool") or ""
            events.append(("call", call_id, tool, _stringify(state.get("input") or {})))
            events.append(("result", call_id, tool, _stringify(state.get("output") or state.get("error") or "")))
    # Pi: the session files are appended as messages complete, so they survive a
    # deadline kill; the filtered stdout stream is the fallback.
    session_dir = agent_dir / "pi" / "sessions"
    pi_sources = sorted(session_dir.rglob("*.jsonl")) if session_dir.is_dir() else []
    if not pi_sources and (agent_dir / "pi.jsonl").is_file():
        pi_sources = [agent_dir / "pi.jsonl"]
    for source in pi_sources:
        for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") in ("message", "message_end"):
                events.extend(_pi_message_events(event.get("message") or {}))
    return events


def is_network_call(tool: str, arguments: str) -> bool:
    lowered = arguments.lower()
    return tool in WEB_TOOLS or any(marker in lowered for marker in NETWORK_ARGUMENT_MARKERS)


def audit_trial(trial_dir: Path, fingerprints: set[str]) -> dict:
    agent_dir = trial_dir / "agent"
    written: set[str] = set()
    network_calls: set[str] = set()
    hidden_hits: dict[str, dict] = {}
    local_hidden: set[str] = set()
    canary_results, source_calls = [], []
    for kind, call_id, tool, text in tool_events(agent_dir):
        lines = _lines(text)
        if kind == "call":
            written |= lines & fingerprints
            if is_network_call(tool, text):
                network_calls.add(call_id)
                lowered = text.lower()
                markers = sorted({marker for marker in SOURCE_MARKERS if marker in lowered})
                if markers:
                    source_calls.append({"call_id": call_id, "tool": tool, "markers": markers,
                                         "args": text[:300]})
            continue
        received = (lines & fingerprints) - written
        if call_id not in network_calls:
            local_hidden |= received
            continue
        # Set order follows the hash seed; sorting keeps the retained evidence reproducible.
        for line in sorted(received):
            hidden_hits.setdefault(line, {"call_id": call_id, "tool": tool})
        if TB_CANARY_GUID in text:
            canary_results.append({"call_id": call_id, "tool": tool})
    denied = []
    egress = trial_dir / "egress.jsonl"
    if egress.is_file():
        for line in egress.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("decision") == "deny":
                denied.append({key: record.get(key) for key in ("phase", "method", "host", "url")})
    flags = [name for name, value in (("hidden_lines", hidden_hits), ("canary_results", canary_results),
                                      ("source_calls", source_calls), ("denied_egress", denied),
                                      ("local_hidden_lines", local_hidden)) if value]
    return {"flags": flags, "strong": any(flag in STRONG_FLAGS for flag in flags),
            "hidden_lines": [dict(line=line[:200], **where) for line, where in list(hidden_hits.items())[:20]],
            "hidden_line_count": len(hidden_hits), "local_hidden_line_count": len(local_hidden),
            "canary_results": canary_results[:10], "source_calls": source_calls[:20], "denied_egress": denied[:20]}


def audit_run(work_dir: Path, tasks_dir: Path) -> dict:
    work_dir, tasks_dir = Path(work_dir), Path(tasks_dir)
    fingerprints: dict[str, set[str]] = {}
    report = {}
    for result_path in sorted(work_dir.glob("trials/*/*/attempt-*/trial/result.json")):
        trial_dir = result_path.parent
        arm, task, attempt = result_path.parts[-5], result_path.parts[-4], result_path.parts[-3]
        if task not in fingerprints:
            fingerprints[task] = task_fingerprints(tasks_dir / task)
        entry = audit_trial(trial_dir, fingerprints[task])
        entry["reward"] = json.loads(result_path.read_text(encoding="utf-8")).get("reward")
        report[f"{arm}/{task}/{attempt}"] = entry
    flagged = {key: value for key, value in report.items() if value["strong"]}
    summary = {"trials": len(report), "flagged": len(flagged),
               "flagged_passes": sorted(key for key, value in flagged.items() if (value["reward"] or 0) >= 1.0),
               "report": report}
    (work_dir / "audit.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


if __name__ == "__main__":
    result = audit_run(Path(sys.argv[1]), Path(sys.argv[2]))
    print(json.dumps({key: result[key] for key in ("trials", "flagged", "flagged_passes")}, indent=2))
