#!/usr/bin/env python3
"""Adversarial, independent history oracle for the Rust Harness graph prototype.

Runs the same JSONL protocol against native and SQLite, keeping expected branch
histories, epochs, immutable node hashes and command receipts in this process.
The oracle is outside the measured Rust work. Outputs are synthetic fixtures,
raw request/response records and logical row counters, never Harness task scores
or physical I/O measurements. No production database or network is accessed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import selectors
import subprocess
import tempfile
import time
from typing import Any


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def frame_id(scope: dict[str, int], parent: str | None, depth: int, delta: str) -> str:
    """Independent implementation of the published header encoding, not Rust output."""
    header = {"scope": {"tenant": scope["tenant"], "owner": scope["owner"]},
              "parent": parent, "depth": depth, "projection_version": 1,
              "delta_sha256": hashlib.sha256(delta.encode("utf-8")).hexdigest()}
    return hashlib.sha256(encoded(header)).hexdigest()


class Rejected(ValueError):
    pass


class HistoryModel:
    """Expected vectors are retained independently of the backend's stored graph."""

    def __init__(self) -> None:
        self.branches: dict[tuple[int, int, str], dict[str, Any]] = {}
        self.receipts: dict[tuple[int, int, str], tuple[dict[str, Any], dict[str, Any]]] = {}

    @staticmethod
    def key(scope: dict[str, int], branch: str) -> tuple[int, int, str]:
        return scope["tenant"], scope["owner"], branch

    def branch(self, scope: dict[str, int], name: str) -> dict[str, Any]:
        try:
            return self.branches[self.key(scope, name)]
        except KeyError as error:
            raise Rejected("missing branch") from error

    def token(self, scope: dict[str, int], name: str) -> dict[str, Any]:
        return {"scope": dict(scope), "branch": name,
                "head": copy.deepcopy(self.branch(scope, name)["head"]), "projection_version": 1}

    def witnessed(self, scope: dict[str, int], token: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        if token["scope"] != scope or token["projection_version"] != 1:
            raise Rejected("scope or projection mismatch")
        branch = self.branch(scope, token["branch"])
        head = token["head"]
        if head["epoch"] != branch["head"]["epoch"]:
            raise Rejected("epoch invalidated")
        witness = branch["witnesses"].get(head["generation"])
        if witness is None or witness[0] != head:
            raise Rejected("caller cannot invent a committed witness")
        return witness[1]

    def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        scope, name, op = request["scope"], request["branch"], request["op"]
        command = request.get("command_id")
        if not scope["tenant"] or not scope["owner"] or not name or len(name.encode()) > 256:
            raise Rejected("invalid identity")
        if command is not None and (not command or len(command.encode()) > 256):
            raise Rejected("invalid command")
        receipt_key = self.key(scope, command) if command is not None else None
        if receipt_key in self.receipts:
            previous, result = self.receipts[receipt_key]
            if previous != request:
                raise Rejected("command reused for another operation")
            return copy.deepcopy(result)
        key = self.key(scope, name)
        if op == "create":
            if key in self.branches:
                raise Rejected("branch exists")
            history: tuple[tuple[str, str], ...] = ()
            head = {"node": None, "depth": 0, "generation": 0, "epoch": 0}
        elif op == "fork":
            history = self.witnessed(scope, request["source"])
            if key in self.branches:
                raise Rejected("branch exists")
            head = dict(request["source"]["head"], generation=0, epoch=0)
        elif op in {"append", "reset", "adopt"}:
            branch = self.branch(scope, name)
            previous = branch["head"]
            if request["expected"] != previous:
                raise Rejected("CAS conflict")
            if op == "adopt":
                history = self.witnessed(scope, request["source"])
                head = dict(request["source"]["head"], generation=previous["generation"] + 1,
                            epoch=previous["epoch"] + 1)
            else:
                if op == "reset" and request["reason"] not in {"edit", "delete", "compaction"}:
                    raise Rejected("unknown reset semantics")
                delta = request["delta"]
                if len(delta.encode()) > 1024 * 1024:
                    raise Rejected("frame bound")
                history = branch["history"] if op == "append" else ()
                depth = len(history) + 1
                parent = history[-1][0] if history else None
                node = frame_id(scope, parent, depth, delta)
                history = history + ((node, delta),)
                head = {"node": node, "depth": depth, "generation": previous["generation"] + 1,
                        "epoch": previous["epoch"] + (op == "reset")}
        elif op == "head":
            return {"token": self.token(scope, name)}
        elif op == "context":
            branch = self.branch(scope, name)
            history = branch["history"]
            anchor = request.get("after")
            prefix = ()
            if anchor is not None:
                if anchor["branch"] != name:
                    raise Rejected("wrong branch")
                prefix = self.witnessed(scope, anchor)
                if history[:len(prefix)] != prefix:
                    raise Rejected("not an ancestor")
            fragments = [delta for _, delta in history[len(prefix):]]
            result = {"token": self.token(scope, name), "fragments": fragments}
            max_fragments = request.get("max_fragments", 4096)
            max_bytes = request.get("max_bytes", 8 * 1024 * 1024)
            if not 1 <= max_fragments <= 131072 or not 1 <= max_bytes <= 64 * 1024 * 1024:
                raise Rejected("invalid output budget")
            if len(fragments) > max_fragments or len(encoded(result)) > max_bytes:
                raise Rejected("output budget exceeded")
            return result
        else:
            raise Rejected("unknown operation")
        witnesses = dict(self.branches.get(key, {}).get("witnesses", {}))
        witnesses[head["generation"]] = (copy.deepcopy(head), history)
        self.branches[key] = {"head": head, "history": history, "witnesses": witnesses}
        result = {"token": self.token(scope, name)}
        assert receipt_key is not None
        self.receipts[receipt_key] = (copy.deepcopy(request), copy.deepcopy(result))
        return result


class GraphProcess:
    def __init__(self, binary: Path, backend: str, path: Path, log: Path) -> None:
        self.args = [str(binary), backend, str(path)]
        self.log = log.open("ab")
        self.process: subprocess.Popen[str] | None = None
        self.pids: list[int] = []
        self.crashes: list[dict[str, int]] = []
        self.pending = bytearray()
        self.start()

    def start(self) -> None:
        self.process = subprocess.Popen(self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.log, text=True, encoding="utf-8", bufsize=1)
        self.pids.append(self.process.pid)
        self.pending.clear()

    def exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        assert self.process is not None and self.process.stdin and self.process.stdout
        self.process.stdin.write(encoded(request).decode() + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + 30
        with selectors.DefaultSelector() as ready:
            ready.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not ready.select(timeout=remaining):
                    raise TimeoutError("graph process did not finish a response within 30 seconds")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError(f"graph process exited before response: {self.process.poll()}")
                self.pending.extend(chunk)
                if len(self.pending) > 65 * 1024 * 1024:
                    raise ValueError("graph response exceeds the protocol maximum plus envelope")
        line, _, remainder = self.pending.partition(b"\n")
        self.pending = remainder
        return json.loads(line)

    def stop(self, crash: bool = False) -> None:
        if self.process is None:
            return
        process = self.process
        if crash:
            process.kill()
        elif process.stdin:
            process.stdin.close()
        try:
            returncode = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
            raise
        finally:
            if process.stdout:
                process.stdout.close()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            self.process = None
        if crash:
            assert returncode == -9, ("SIGKILL recovery was not exercised", returncode)
            self.crashes.append({"pid": process.pid, "returncode": returncode})
        elif returncode != 0:
            raise RuntimeError(f"graph process failed at EOF: {returncode}")

    def reopen(self, crash: bool = False) -> None:
        self.stop(crash=crash)
        self.start()

    def close(self) -> None:
        try:
            self.stop()
        finally:
            self.log.close()


class Verifier:
    def __init__(self, client: GraphProcess, output: Any) -> None:
        self.client, self.output = client, output
        self.model = HistoryModel()
        self.serial = 0
        self.accepted = self.rejected = 0
        self.work = {key: 0 for key in ("reads", "writes", "read_bytes", "write_bytes", "node_reads", "header_reads")}

    def command(self, op: str, scope: dict[str, int], branch: str, **fields: Any) -> dict[str, Any]:
        self.serial += 1
        return {"op": op, "scope": scope, "branch": branch,
                "command_id": f"command-{self.serial}", **fields}

    def check(self, request: dict[str, Any], label: str) -> dict[str, Any]:
        rejected = False
        before = copy.deepcopy(self.model)
        try:
            expected = self.model.apply(request)
        except Rejected:
            expected, rejected = None, True
        response = self.client.exchange(request)
        record = {"label": label, "request": request, "response": response, "expected": expected,
                  "expected_rejection": rejected, "pid": self.client.process.pid}
        self.output.write(encoded(record).decode() + "\n")
        self.output.flush()
        assert response.get("ok") is (not rejected), record
        if rejected:
            self.rejected += 1
            assert self.model.branches == before.branches and self.model.receipts == before.receipts
            return response
        self.accepted += 1
        assert response["value"]["result"] == expected, record
        work = response["value"]["work"]
        assert set(work) == set(self.work) and all(type(value) is int and value >= 0 for value in work.values()), record
        for key, value in work.items():
            self.work[key] += value
        op = request["op"]
        is_replay = before.key(request["scope"], request.get("command_id", "")) in before.receipts
        if is_replay:
            assert work["writes"] == 0 and work["reads"] == 1, record
        elif op == "fork":
            assert work["writes"] == 3 and work["node_reads"] == work["header_reads"] == 0, record
        elif op == "context":
            assert work["writes"] == 0, record
            assert work["node_reads"] == len(expected["fragments"]), record
            assert work["header_reads"] == (expected["token"]["head"]["depth"] if request.get("verify_prefix") else 0), record
        return response

    def token(self, scope: dict[str, int], branch: str) -> dict[str, Any]:
        return self.model.token(scope, branch)

    def append(self, scope: dict[str, int], branch: str, delta: str) -> dict[str, Any]:
        request = self.command("append", scope, branch, expected=self.token(scope, branch)["head"], delta=delta)
        self.check(request, "append")
        return request

    def context(self, scope: dict[str, int], branch: str, after: dict[str, Any] | None = None,
                **fields: Any) -> dict[str, Any]:
        return {"op": "context", "scope": scope, "branch": branch, "after": after, **fields}


def adversarial_cases(verifier: Verifier) -> None:
    scope = {"tenant": 7, "owner": 11}
    for current in [scope, {"tenant": 7, "owner": 12}, {"tenant": 8, "owner": 11},
                    {"tenant": 2**64 - 1, "owner": 2**64 - 1}]:
        verifier.check(verifier.command("create", current, "main"), "scope_create")
        verifier.append(current, "main", "first/\\\"\n\t\u0000汉字😀")
        verifier.check(verifier.context(current, "main"), "exact_scope_history")
    anchor = verifier.token(scope, "main")
    request = verifier.append(scope, "main", "second")
    verifier.check(request, "identical_command_replay")
    verifier.check(dict(request, delta="changed"), "conflicting_command_replay")
    verifier.check(verifier.command("append", scope, "main", expected=anchor["head"], delta="stale"), "stale_CAS")
    for verify_prefix in [False, True]:
        verifier.check(verifier.context(scope, "main", anchor, verify_prefix=verify_prefix), "same_suffix_two_algorithms")
    for field, value in [("depth", 0), ("depth", 999), ("generation", 0), ("generation", 999),
                         ("node", None), ("node", "0" * 64), ("epoch", 123)]:
        forged = copy.deepcopy(anchor)
        forged["head"][field] = value
        verifier.check(verifier.context(scope, "main", forged), "forged_" + field)
    for patch in [{"scope": {"tenant": 7, "owner": 12}}, {"projection_version": 2}, {"branch": "other"}]:
        verifier.check(verifier.context(scope, "main", dict(anchor, **patch)), "forged_token_identity")
    verifier.check(verifier.context({"tenant": 0, "owner": 11}, "main"), "zero_scope_rejected")
    for name in ["left", "right"]:
        verifier.check(verifier.command("fork", scope, name, source=anchor), "shared_prefix_fork")
        verifier.append(scope, name, name + " independent suffix")
    right = verifier.token(scope, "right")
    forged = dict(right, branch="left")
    verifier.check(verifier.context(scope, "left", forged), "same_depth_different_ancestor")
    verifier.check(verifier.context(scope, "main", right), "foreign_branch_token")
    old_left = verifier.token(scope, "left")
    verifier.check(verifier.command("adopt", scope, "left", expected=old_left["head"], source=right), "adopt")
    verifier.check(verifier.context(scope, "left", old_left), "adopt_invalidates_old_epoch")
    for reason in ["edit", "delete", "compaction"]:
        old = verifier.token(scope, "main")
        verifier.check(verifier.command("reset", scope, "main", expected=old["head"], delta="same root", reason=reason), "reset_" + reason)
        verifier.check(verifier.context(scope, "main", old), "reset_invalidates_old_epoch")
        if reason != "edit":
            assert old["head"]["node"] == verifier.token(scope, "main")["head"]["node"], "ABA fixture did not repeat node hash"
    verifier.check(request, "old_receipt_replay_after_reset")
    verifier.check({"op": "head", "scope": scope, "branch": "main"}, "replay_did_not_rewind_head")
    verifier.check(verifier.context(scope, "main", anchor), "old_receipt_is_not_current_proof")
    current = verifier.context(scope, "main")
    expected_bytes = len(encoded(verifier.model.apply(current)))
    verifier.check(dict(current, max_bytes=expected_bytes), "exact_JSON_budget")
    verifier.check(dict(current, max_bytes=expected_bytes - 1), "one_byte_under_JSON_budget")
    for limit in [0, 131073]:
        verifier.check(dict(current, max_fragments=limit), "invalid_fragment_budget")
    verifier.append(scope, "main", "second after reset")
    verifier.check(dict(current, max_fragments=1), "fragment_budget_exhausted")
    verifier.check(current, "failed_reads_did_not_change_head")
    verifier.client.reopen(crash=True)
    verifier.check(request, "acknowledged_receipt_survives_SIGKILL")
    for tenant, owner, branch in verifier.model.branches:
        verifier.check(verifier.context({"tenant": tenant, "owner": owner}, branch), "acknowledged_history_survives_SIGKILL")
    verifier.client.reopen()
    verifier.check(current, "clean_reopen")


def randomized_cases(verifier: Verifier, seed: int, steps: int) -> None:
    rng = random.Random(seed)
    scope = {"tenant": 7, "owner": 11}
    remembered: list[dict[str, Any]] = []
    for step in range(steps):
        names = [name for tenant, owner, name in verifier.model.branches if (tenant, owner) == (7, 11)]
        branch = rng.choice(names)
        token = verifier.token(scope, branch)
        remembered.append(token)
        op = rng.choice(["append", "append", "fork", "reset", "adopt", "context", "forged"])
        if op == "append":
            verifier.append(scope, branch, f"seed={seed}/step={step}/" + rng.choice(["", "汉字😀", "\\\"\n\t", "repeat"]))
            verifier.check(verifier.context(scope, branch, token, verify_prefix=bool(step % 2)), "random_exact_new_suffix")
        elif op == "fork":
            source = rng.choice(remembered) if step % 3 == 0 else verifier.token(scope, rng.choice(names))
            verifier.check(verifier.command("fork", scope, f"fork-{step}", source=source), "random_fork")
        elif op == "reset":
            verifier.check(verifier.command("reset", scope, branch, expected=token["head"],
                delta=f"reset-{step % 7}", reason=rng.choice(["edit", "delete", "compaction"])), "random_reset")
        elif op == "adopt":
            verifier.check(verifier.command("adopt", scope, branch, expected=token["head"],
                source=rng.choice(remembered) if step % 3 == 0 else verifier.token(scope, rng.choice(names))), "random_adopt")
        elif op == "forged":
            forged = copy.deepcopy(token)
            forged["head"][rng.choice(["depth", "generation", "epoch"])] += 1
            verifier.check(verifier.context(scope, branch, forged), "random_forged_cursor")
        else:
            after = rng.choice(remembered + [None])
            verifier.check(verifier.context(scope, branch, after, verify_prefix=bool(step % 2)), "random_context")
        if step % 17 == 0:
            verifier.check(verifier.context(scope, branch), "random_exact_full_history")
    for tenant, owner, branch in verifier.model.branches:
        current_scope = {"tenant": tenant, "owner": owner}
        verifier.check(verifier.context(current_scope, branch), "final_exact_history")
        verifier.check(verifier.context(current_scope, branch, verifier.token(current_scope, branch)), "final_empty_suffix")


def run(binary: Path, output: Path, backends: list[str], seed: int, steps: int) -> dict[str, Any]:
    if os.name != "posix":
        raise RuntimeError("this verifier requires POSIX SIGKILL recovery")
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"binary": str(binary), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "seed": seed, "steps": steps, "scope": "synthetic immutable graph; no mutable Turn integration, external effects, task success or physical I/O verdict",
        "authorization_boundary": "scope isolation with trusted caller identity; this prototype has no transport authentication",
        "backends": {}}
    for backend in backends:
        with tempfile.TemporaryDirectory(prefix=f"harness-{backend}-") as directory:
            path = Path(directory) / ("store.sqlite" if backend == "sqlite" else "store")
            client = GraphProcess(binary, backend, path, output / f"{backend}.stderr.jsonl")
            try:
                with (output / f"{backend}.raw.jsonl").open("w", encoding="utf-8") as raw:
                    verifier = Verifier(client, raw)
                    adversarial_cases(verifier)
                    randomized_cases(verifier, seed, steps)
                    report["backends"][backend] = {"accepted": verifier.accepted, "rejected": verifier.rejected,
                        "work": verifier.work, "pids": client.pids, "crashes": client.crashes,
                        "branches": len(verifier.model.branches), "receipts": len(verifier.model.receipts),
                        "open_store_file_bytes": sum(item.stat().st_size for item in Path(directory).rglob("*") if item.is_file())}
            finally:
                client.close()
            configurations = [json.loads(line)["configuration"] for line in
                (output / f"{backend}.stderr.jsonl").read_text(encoding="utf-8").splitlines()
                if line.startswith("{") and json.loads(line).get("backend") == backend]
            assert len(configurations) == len(client.pids), "missing actual startup configuration"
            assert all(value == configurations[0] for value in configurations), "configuration changed after reopen"
            configuration = configurations[0]
            if backend == "sqlite":
                assert configuration["journal_mode"] == "wal" and configuration["synchronous"] == 2
                assert configuration["cache_size"] == -49152 and configuration["foreign_keys"] == 1
            else:
                assert configuration["wal_durability"] == "Strict"
                assert configuration["block_cache_default_bytes"] == 32 * 1024 * 1024
                assert configuration["page_cache_default_bytes"] == 16 * 1024 * 1024
            report["backends"][backend]["configuration"] = configuration
    reports = list(report["backends"].values())
    if len(reports) > 1:
        for field in ["accepted", "rejected", "work", "branches", "receipts"]:
            assert all(item[field] == reports[0][field] for item in reports), ("backend unequal logical work", field)
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--backends", nargs="+", choices=["native", "sqlite"], default=["native", "sqlite"])
    parser.add_argument("--seed", type=int, default=20261019)
    parser.add_argument("--steps", type=int, default=256)
    args = parser.parse_args()
    if not 0 <= args.steps <= 10000:
        parser.error("--steps must be 0..10000; this is a bounded correctness verifier")
    report = run(args.binary.resolve(), args.output.resolve(), args.backends, args.seed, args.steps)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
