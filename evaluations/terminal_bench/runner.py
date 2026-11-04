"""Native Terminal-Bench runner: one CodeLab sandbox VM per trial, no Harbor.

Responsibility: plan trials (task x arm), run them concurrently in sandbox VMs
through the ingress native exec channel, persist every attempt so an
interrupted run resumes without recomputing measured trials, recover evidence
bundles and write ``summary.json``. Unmeasured trials (start/submit failures,
lost VMs, runner infra errors) are retried in new attempts and are never
counted as measured.

Layout under ``--work-dir``: ``run.json`` (frozen configuration),
``trials/<arm>/<task>/attempt-<n>/{state.json,spec.json,result.json,bundle.tar.gz,trial/}``,
``summary.json``.

Entry point: ``python -m evaluations.terminal_bench run|summarize``.
Dependencies: ``evaluations.terminal_bench.harness`` (specs) and ``.gateway``
(pssdk sessions); the sandbox secret token comes from ``SANDBOX_<cluster>_TOKEN``.
"""
from __future__ import annotations

import json
import os
import re
import tarfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from evaluations.terminal_bench import harness
from evaluations.terminal_bench.gateway import SESSION_ABSENT, SandboxSession, remote_error_text, start_body

VM_TRIAL = Path(__file__).with_name("vm_trial.py")
MEASURED = ("scored", "no_reward")
# Liveness budgets (not tuning): start tries per attempt, consecutive failed polls.
START_TRIES = 20
POLL_FAILURE_LIMIT = 40
# Overhead beyond agent + verifier budgets: image pull, install, teardown.
TRIAL_OVERHEAD_SEC = 3600
_CLUSTER = re.compile(r"/system/([^/]+)/?$")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def token_env_for(endpoint: str) -> str:
    match = _CLUSTER.search(endpoint)
    if not match:
        raise SystemExit(f"cannot derive SANDBOX_<cluster>_TOKEN from endpoint {endpoint!r}")
    return f"SANDBOX_{match.group(1)}_TOKEN"


def extract_regular_files(archive: Path, destination: Path) -> int:
    """Unpack only regular files and directories inside destination; returns skipped members."""
    skipped = 0
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if not (member.isfile() or member.isdir()) or not str(target).startswith(str(root) + os.sep):
                skipped += 1
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            with open(target, "wb") as handle:
                while chunk := source.read(1 << 20):
                    handle.write(chunk)
    return skipped


@dataclass
class RunConfig:
    work_dir: Path
    tasks_dir: Path
    tasks: list[str]
    arms: list[str]
    protocol: str
    model: str
    relay_url: str
    files_url: str
    artifacts_manifest: Path
    endpoint: str
    image: str
    branch: str
    proxy: str
    max_alive_min: int
    max_attempts: int
    context_tokens: int
    poll_interval_sec: int
    run_label: str

    def to_json(self) -> dict:
        return {key: str(value) if isinstance(value, Path) else value for key, value in self.__dict__.items()}


class Runner:
    def __init__(self, config: RunConfig):
        self.config = config
        self.protocol = harness.PROTOCOLS[config.protocol]
        self.artifacts = harness.load_artifacts(config.artifacts_manifest)
        self.tasks = {task.name: task for task in harness.list_tasks(config.tasks_dir, config.tasks)}
        token_env = token_env_for(config.endpoint)
        self.token = os.environ.get(token_env, "").strip()
        if not self.token:
            raise SystemExit(f"${token_env} is empty; inject the sandbox application secret_token")
        self.vm_trial = VM_TRIAL.read_bytes()
        self.lock = threading.Lock()
        self.stop_requested = threading.Event()

    # ------------------------------------------------------------ specs
    def agent_for(self, task: harness.Task, arm: str) -> dict:
        model_base_url = f"{self.config.relay_url.rstrip('/')}/arm/{arm}-{self.config.run_label}/v1"
        timeout = harness.agent_timeout_for(task, self.protocol)
        if arm == "tofu":
            return harness.tofu_agent(artifact=self.artifacts["tofu"], files_url=self.config.files_url,
                                      model_base_url=model_base_url, model=self.config.model,
                                      timeout_sec=timeout, instruction=task.instruction,
                                      context_tokens=self.config.context_tokens,
                                      output_tokens=self.protocol.max_tokens)
        if arm == "opencode":
            return harness.opencode_agent(artifact=self.artifacts["opencode"], files_url=self.config.files_url,
                                          model_base_url=model_base_url, model=self.config.model,
                                          instruction=task.instruction, context_tokens=self.config.context_tokens,
                                          output_tokens=self.protocol.max_tokens)
        if arm == "pi":
            return harness.pi_agent(artifact=self.artifacts["pi"], files_url=self.config.files_url,
                                    model_base_url=model_base_url, model=self.config.model,
                                    instruction=task.instruction, context_tokens=self.config.context_tokens,
                                    output_tokens=self.protocol.max_tokens)
        raise ValueError(f"unknown arm {arm!r}")

    def spec_for(self, task: harness.Task, arm: str, attempt: int) -> dict:
        return harness.build_trial_spec(
            task=task, arm=arm, attempt=attempt, agent=self.agent_for(task, arm), protocol=self.protocol,
            agent_timeout_sec=harness.agent_timeout_for(task, self.protocol), egress_upstream=self.config.proxy)

    # ------------------------------------------------------------ logging
    def log(self, trial: str, message: str) -> None:
        with self.lock:
            print(f"{now()} {trial} {message}", flush=True)

    # ------------------------------------------------------------ one trial
    def trial_dir(self, task: str, arm: str) -> Path:
        return self.config.work_dir / "trials" / arm / task

    def run_trial(self, task_name: str, arm: str) -> dict:
        task = self.tasks[task_name]
        label = f"{task_name}/{arm}"
        for attempt in range(1, self.config.max_attempts + 1):
            if self.stop_requested.is_set():
                return {"phase": "interrupted"}
            directory = self.trial_dir(task_name, arm) / f"attempt-{attempt}"
            state = read_json(directory / "state.json") or {}
            if state.get("phase") in MEASURED:
                return state
            if state.get("finished"):
                continue  # an unmeasured attempt is immutable; try the next one
            try:
                state = self.run_attempt(task, arm, attempt, directory, state)
            except Exception:
                state = dict(state, phase="runner_error", error=traceback.format_exc()[-3000:])
                self.release(directory, state)
            state["finished"] = True
            write_json(directory / "state.json", state)
            self.log(label, f"attempt {attempt} -> {state.get('phase')} reward={state.get('reward')}")
            if state.get("phase") in MEASURED:
                return state
        return {"phase": "unmeasured"}

    def save(self, directory: Path, state: dict) -> None:
        write_json(directory / "state.json", state)

    def run_attempt(self, task: harness.Task, arm: str, attempt: int, directory: Path, state: dict) -> dict:
        label = f"{task.name}/{arm}#{attempt}"
        spec = read_json(directory / "spec.json")
        if spec is None:
            spec = self.spec_for(task, arm, attempt)
            write_json(directory / "spec.json", spec)
        session = SandboxSession(self.config.endpoint, self.token, session_id=state.get("session_id"))
        if not state.get("exec_id"):
            if state.get("session_id"):
                # Started or submitted before an interruption without a recorded exec: uncertain, release.
                state = dict(state, phase="resume_uncertain")
                self.release(directory, state, session)
                return state
            state = {"phase": "starting", "session_id": session.session_id, "created_at": now(),
                     "trial_id": spec["trial_id"]}
            self.save(directory, state)
            session = self.start_session(session, directory, state, label)
            if session is None:
                return state
            state = self.submit(session, spec, directory, state, label)
            if state["phase"] != "running":
                self.release(directory, state, session)
                return state
        state = self.wait(session, spec, directory, state, label)
        if state["phase"] == "exited":
            state = self.collect(session, spec, directory, state, label)
        self.release(directory, state, session)
        return state

    def start_session(self, session: SandboxSession, directory: Path, state: dict,
                      label: str) -> SandboxSession | None:
        """Start the VM; a failed try is released and retried under a new session id."""
        body = start_body(image=self.config.image, branch=self.config.branch, proxy=self.config.proxy,
                          cpus=self.protocol.cpus, memory=self.protocol.memory,
                          max_alive_min=self.config.max_alive_min)
        for start_try in range(1, START_TRIES + 1):
            if self.stop_requested.is_set():
                state.update(phase="interrupted")
                return None
            try:
                session.start(body)
                state.update(phase="started", started_at=now(), start_tries=start_try)
                self.save(directory, state)
                self.log(label, f"session started ({start_try} tries)")
                return session
            except Exception as error:
                message = remote_error_text(error)[:400]
                state.setdefault("start_errors", []).append({"at": now(), "type": type(error).__name__,
                                                             "message": message})
                self.save(directory, state)
                self.log(label, f"start failed try {start_try}: {type(error).__name__}: {message[:160]}")
                # A start that timed out may still create the sandbox; release before retrying.
                self.try_stop(session)
                session = SandboxSession(self.config.endpoint, self.token)
                state["session_id"] = session.session_id
                self.save(directory, state)
                time.sleep(min(30 * start_try, 300))
        state.update(phase="start_failed")
        return None

    def submit(self, session: SandboxSession, spec: dict, directory: Path, state: dict, label: str) -> dict:
        root = harness.VM_ROOT + "/" + spec["trial_key"]
        files = {f"{root}/vm_trial.py": self.vm_trial,
                 f"{root}/spec.json": json.dumps(spec).encode(),
                 spec["task_bundle"]: harness.bundle_task(self.tasks[spec["task_name"]])}
        state.update(phase="submitting")
        self.save(directory, state)
        try:
            reply = session.exec(["python3", f"{root}/vm_trial.py", f"{root}/spec.json"], files=files)
        except Exception as error:
            state.update(phase="submit_error", error=f"{type(error).__name__}: {remote_error_text(error)[:600]}")
            return state
        if not isinstance(reply, dict) or not reply.get("exec_id"):
            state.update(phase="submit_error", error=f"no exec_id: {str(reply)[:600]}")
            return state
        state.update(phase="running", exec_id=reply["exec_id"], submitted_at=now(), submitted_epoch=time.time())
        self.save(directory, state)
        self.log(label, f"submitted exec {reply['exec_id']}")
        return state

    def wait(self, session: SandboxSession, spec: dict, directory: Path, state: dict, label: str) -> dict:
        budget = (spec["agent_timeout_sec"] + spec["agent"].get("deadline_grace_sec", 0)
                  + spec["verifier_timeout_sec"] * spec["verifier_attempts"] + TRIAL_OVERHEAD_SEC)
        deadline = state.get("submitted_epoch", time.time()) + budget
        failures = 0
        while True:
            if self.stop_requested.is_set():
                state.update(phase="interrupted")
                return state
            try:
                status = session.status(state["exec_id"])
                failures = 0
            except Exception as error:
                message = remote_error_text(error)
                if SESSION_ABSENT in message or "exec not found" in message \
                        or type(error).__name__ == "SandboxInstanceUnreachableError":
                    state.update(phase="lost", error=message[:600], lost_at=now())
                    return state
                failures += 1
                state["last_poll_error"] = f"{type(error).__name__}: {message[:300]}"
                if failures >= POLL_FAILURE_LIMIT:
                    state.update(phase="lost", error=state["last_poll_error"], lost_at=now())
                    return state
                time.sleep(self.config.poll_interval_sec)
                continue
            if status.get("state") == "exited":
                state.update(phase="exited", exit_code=status.get("exit_code"), exited_at=now(),
                             runner_stdout_tail=status.get("stdout_tail", "")[-2000:],
                             runner_stderr_tail=status.get("stderr_tail", "")[-4000:])
                self.save(directory, state)
                return state
            phase_line = (status.get("stderr_tail") or "").strip().splitlines()[-1:] or [""]
            if phase_line[0] != state.get("vm_last_line"):
                state["vm_last_line"] = phase_line[0]
                self.save(directory, state)
            if time.time() > deadline:
                try:
                    session.kill(state["exec_id"], force=True)
                except Exception:
                    pass
                state.update(phase="deadline", error=f"trial exceeded {budget}s")
                return state
            time.sleep(self.config.poll_interval_sec)

    def collect(self, session: SandboxSession, spec: dict, directory: Path, state: dict, label: str) -> dict:
        try:
            result = json.loads(session.read_file(spec["out_dir"] + "/result.json"))
        except Exception as error:
            state.update(phase="runner_crashed", error=f"result.json unavailable: {remote_error_text(error)[:400]}")
            return state
        write_json(directory / "result.json", result)
        try:
            bundle = session.read_file(spec["out_dir"] + ".bundle.tar.gz")
            (directory / "bundle.tar.gz").write_bytes(bundle)
            state["bundle_skipped_members"] = extract_regular_files(directory / "bundle.tar.gz", directory)
            state["bundle_bytes"] = len(bundle)
        except Exception as error:
            state["bundle_error"] = f"{type(error).__name__}: {remote_error_text(error)[:400]}"
        outcome = result.get("outcome")
        state.update(reward=result.get("reward"), outcome=outcome, infra_error=result.get("infra_error"))
        state["phase"] = outcome if outcome in MEASURED else "infra_error"
        self.log(label, f"outcome={outcome} reward={result.get('reward')}")
        return state

    # ------------------------------------------------------------ release
    def try_stop(self, session: SandboxSession) -> str | None:
        try:
            return session.stop()
        except Exception:
            return None

    def release(self, directory: Path, state: dict, session: SandboxSession | None = None) -> None:
        if state.get("stopped") or not state.get("session_id"):
            return
        session = session or SandboxSession(self.config.endpoint, self.token, session_id=state["session_id"])
        for _ in range(3):
            outcome = self.try_stop(session)
            if outcome:
                state.update(stopped=True, stop_outcome=outcome, stopped_at=now())
                break
            time.sleep(5)
        else:
            state["stop_failed"] = True
        self.save(directory, state)

    # ------------------------------------------------------------ whole run
    def run(self, concurrency: int) -> dict:
        plan = [(task, arm) for task in sorted(self.tasks) for arm in self.config.arms]
        self.log("run", f"{len(plan)} trials, concurrency {concurrency}, protocol {self.protocol.name}")
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(self.run_trial, task, arm) for task, arm in plan]
            try:
                for future in futures:
                    future.result()
            except KeyboardInterrupt:
                self.stop_requested.set()
                raise
        return summarize(self.config.work_dir)


def summarize(work_dir: Path) -> dict:
    work_dir = Path(work_dir)
    config = read_json(work_dir / "run.json") or {}
    arms: dict[str, dict] = {}
    rows = []
    for state_path in sorted(work_dir.glob("trials/*/*/attempt-*/state.json")):
        arm, task, attempt = state_path.parts[-4], state_path.parts[-3], state_path.parts[-2]
        rows.append((arm, task, int(attempt.split("-")[1]), read_json(state_path)))
    latest: dict[tuple[str, str], tuple[int, dict]] = {}
    for arm, task, attempt, state in rows:
        if (arm, task) not in latest or attempt > latest[(arm, task)][0]:
            latest[(arm, task)] = (attempt, state)
    planned = config.get("tasks") or sorted({task for _, task, _, _ in rows})
    trials = {}
    for arm in config.get("arms") or sorted({arm for arm, _, _, _ in rows}):
        counts = {"planned": len(planned), "measured": 0, "passed": 0, "no_reward": 0, "unmeasured": 0,
                  "in_progress": 0, "unstopped_sessions": 0}
        for task in planned:
            attempt, state = latest.get((arm, task), (0, {}))
            phase = state.get("phase")
            if phase in MEASURED:
                counts["measured"] += 1
                counts["no_reward"] += phase == "no_reward"
                counts["passed"] += (state.get("reward") or 0) >= 1.0
            elif state.get("finished") and attempt >= int(config.get("max_attempts") or 1):
                counts["unmeasured"] += 1
            else:
                counts["in_progress"] += 1
            trials[f"{arm}/{task}"] = {"attempt": attempt, "phase": phase, "reward": state.get("reward")}
        counts["unstopped_sessions"] = sum(1 for a, _, _, s in rows if a == arm
                                           and s.get("session_id") and not s.get("stopped")
                                           and s.get("finished"))
        counts["pass_rate_of_planned"] = round(counts["passed"] / counts["planned"], 4) if counts["planned"] else None
        counts["pass_rate_of_measured"] = (round(counts["passed"] / counts["measured"], 4)
                                           if counts["measured"] else None)
        arms[arm] = counts
    summary = {"generated_at": now(), "protocol": config.get("protocol"), "arms": arms, "trials": trials}
    write_json(work_dir / "summary.json", summary)
    return summary
