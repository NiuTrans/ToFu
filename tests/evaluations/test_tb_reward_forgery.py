"""The native Terminal-Bench reward channel is root's alone (offline specs).

Covered here: ``/logs/verifier`` is an empty root:root 0700 directory before
the agent and again before the verifier, verifier commands run as uid 0,
``/logs/agent`` belongs to the image's user at 0755, and rewards are read only
from regular files. ``tests/support/reward_forgery_probe.py`` is the uid-65534
attacker, run against the host bind source (the same DAC decision as the
container's ``/logs/verifier``); a real in-container trial needs the VM smoke.

The permission layout needs real root, or a single-uid user namespace for the
parts that only chown to 0:0; only real root can fork a uid-65534 child.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from evaluations.terminal_bench import vm_trial
from tests.support import reward_forgery_probe as probe

pytestmark = pytest.mark.unit

USERNS_ENV = "TOFU_TB_FORGERY_IN_USERNS"


def make_trial(tmp_path, agent_uid=0, agent_gid=0):
    """A trial whose ``docker`` records argv and runs the in-container user probe as a shell."""
    spec = {"out_dir": str(tmp_path / "out"), "task_dir": str(tmp_path / "task"), "trial_key": "k",
            "trial_id": "t", "task_name": "x", "arm": "tofu", "image": "example/demo:1",
            "cpus": "4", "memory": "16g", "agent": {"kind": "tofu"}, "task_env": {},
            "egress": {"no_proxy": "127.0.0.1"}, "verifier_attempts": 1, "verifier_timeout_sec": 1}
    (tmp_path / "task" / "tests").mkdir(parents=True)
    (tmp_path / "task" / "tests" / "test.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.jsonl"
    fake = (
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        f"with open({str(calls)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'exec' and sys.argv[-1].startswith('echo \"$HOME|'):\n"
        "    sys.stdout.flush()\n"
        "    sys.exit(subprocess.call(['sh'] + sys.argv[-2:]))\n"
    )
    (bin_dir / "docker").write_text(fake, encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "id").write_text(f"#!/bin/sh\ncase $1 in -u) echo {agent_uid};; -g) echo {agent_gid};; esac\n",
                                encoding="utf-8")
    (bin_dir / "id").chmod(0o755)
    trial = vm_trial.Trial(spec)
    trial.network = "bridge"
    trial.proxy = type("Proxy", (), {"address": "127.0.0.1:1", "phase": "setup"})()
    return trial, calls


def read_calls(calls):
    return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]


def exec_calls(calls, marker):
    return [argv for argv in read_calls(calls) if argv[0] == "exec" and any(marker in part for part in argv)]


def blocked_write(directory):
    """Result of trying to write a file as the probe's current uid."""
    return probe.outcome(lambda: probe.write_text(str(Path(directory) / "other-uid.txt"), "x"))


def host_side_permissions(agent_uid, agent_gid):
    """Run start_container → a tampering agent phase → run_verifier → read_reward; return observations."""
    with tempfile.TemporaryDirectory(prefix="tb-forgery-flow-") as root:
        tmp_path = Path(root)
        trial, calls = make_trial(tmp_path, agent_uid, agent_gid)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{old_path}"
        try:
            trial.start_container()
            started = {}
            for name in ("verifier", "agent"):
                info = os.lstat(trial.out / name)
                started[name] = {"uid": info.st_uid, "gid": info.st_gid, "mode": oct(info.st_mode & 0o7777)}
            outside = tmp_path / "outside"
            outside.mkdir()
            forged = outside / "forged.txt"
            forged.write_text("1.0\n", encoding="utf-8")
            verifier = trial.out / "verifier"
            verifier.chmod(0o777)
            (verifier / "reward.txt").write_text("1.0\n", encoding="utf-8")
            (verifier / "reward.json").symlink_to(forged)
            (verifier / ".forged-swap.txt").write_text("1.0\n", encoding="utf-8")
            (verifier / "planted").mkdir()
            os.mkfifo(verifier / "planted.fifo")
            trial.run_verifier()
            reward = trial.read_reward()
            return {
                "started": started,
                "reward": reward,
                "verifier_entries": sorted(os.listdir(verifier)),
                "verifier_mode": oct(os.lstat(verifier).st_mode & 0o7777),
                "verifier_exec": exec_calls(calls, "tbn-verifier"),
                "agent_exec": exec_calls(calls, "tbn-agent"),
                "outside_intact": forged.read_text(encoding="utf-8") == "1.0\n" and not forged.is_symlink(),
                "phase_ok": trial.result["phases"]["verifier"]["ok"],
            }
        finally:
            os.environ["PATH"] = old_path


def test_rewards_are_read_from_regular_files_only(tmp_path):
    trial, _ = make_trial(tmp_path)
    (tmp_path / "outside").mkdir()
    forged = tmp_path / "outside" / "forged.txt"
    forged.write_text("1.0\n", encoding="utf-8")
    verifier = tmp_path / "out" / "verifier"
    verifier.mkdir(parents=True)
    (verifier / "reward.txt").symlink_to(forged)
    (verifier / "reward.json").write_text('{"reward": 0.25}', encoding="utf-8")
    assert trial.read_reward() == (0.25, "reward.json")
    (verifier / "reward.txt").unlink()
    (verifier / "reward.json").unlink()
    (verifier / "reward.json").symlink_to(forged)
    assert trial.read_reward() == (None, None)
    (verifier / "reward.json").unlink()
    os.mkfifo(verifier / "reward.txt")
    assert trial.read_reward() == (None, None), "a planted FIFO must not be read (or block the runner)"
    (verifier / "reward.txt").unlink()
    (verifier / "reward.txt").mkdir()
    assert trial.read_reward() == (None, None)
    (verifier / "reward.txt").rmdir()
    (verifier / "reward.txt").write_text("0.5\n", encoding="utf-8")
    assert trial.read_reward() == (0.5, "reward.txt")


def test_verifier_log_dir_is_root_only_and_agent_plantings_are_removed():
    if os.geteuid() != 0:
        if os.environ.get(USERNS_ENV) or shutil.which("unshare") is None:
            pytest.skip("needs root or a working single-uid user namespace")
        # Mapping only my own uid to 0 makes the 0:0 chown work; it cannot drop to 65534.
        completed = subprocess.run(
            ["unshare", "-U", "-r", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "--override-ini", "addopts=", __file__ + "::test_verifier_log_dir_is_root_only_and_agent_plantings_are_removed"],
            env={**os.environ, USERNS_ENV: "1", "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
        assert completed.returncode == 0, completed.stdout
        return
    observed = host_side_permissions(agent_uid=0, agent_gid=0)
    assert observed["started"]["verifier"] == {"uid": 0, "gid": 0, "mode": "0o700"}
    assert observed["started"]["agent"] == {"uid": 0, "gid": 0, "mode": "0o755"}
    assert observed["phase_ok"] is True
    # Every verifier exec is uid 0; the agent keeps the image's user.
    assert observed["verifier_exec"], "no verifier exec was recorded"
    for argv in observed["verifier_exec"]:
        assert argv[1:3] == ["-u", "0"], argv
    for argv in observed["agent_exec"]:
        assert "-u" not in argv, argv
    # The agent's planted reward, symlink, staged swap, directory and FIFO are all gone.
    assert observed["reward"] == (None, None), observed
    assert observed["verifier_entries"] == [], observed
    assert observed["verifier_mode"] == "0o700"
    assert observed["outside_intact"] is True


@pytest.mark.skipif(os.geteuid() != 0, reason="a uid-65534 attacker needs real root; the VM smoke runs this as root")
def test_uid_65534_cannot_forge_reward_and_legitimate_channels_still_work():
    with tempfile.TemporaryDirectory(prefix="tb-forgery-redteam-") as root:
        tmp_path = Path(root)
        os.chmod(tmp_path, 0o755)  # the attacker must be able to traverse to the directories
        trial, _ = make_trial(tmp_path, agent_uid=0, agent_gid=0)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{old_path}"
        try:
            trial.start_container()
        finally:
            os.environ["PATH"] = old_path
        verifier, agent_logs = trial.out / "verifier", trial.out / "agent"
        outside = tmp_path / "outside"
        outside.mkdir()
        outside.chmod(0o777)
        # Control: the de-privileged child really runs, and can write where the mode allows it.
        assert probe.run_as_uid(probe.write_text, 65534, 65534, str(outside / "writable.txt"), "ok\n") is None
        assert (outside / "writable.txt").read_text(encoding="utf-8") == "ok\n"
        # The agent still writes its own logs through its own uid, and only that uid can.
        assert probe.run_as_uid(probe.write_text, 0, 0, str(agent_logs / "tofu-result.json"), "agent-log\n") is None
        assert (agent_logs / "tofu-result.json").read_text(encoding="utf-8") == "agent-log\n"
        assert probe.run_as_uid(blocked_write, 65534, 65534, str(agent_logs))["result"] == "blocked"
        before = probe.run_as_uid(probe.attack, 65534, 65534, str(verifier), str(outside))
        assert (before["actual_uid"], before["actual_gid"]) == (65534, 65534)
        (verifier / "reward.txt").write_text("0.25\n", encoding="utf-8")  # the root verifier's own reward
        after = probe.run_as_uid(probe.attack, 65534, 65534, str(verifier), str(outside))
        assert probe.forgeries(before) == [], json.dumps(before, indent=2, sort_keys=True)
        assert probe.forgeries(after) == [], json.dumps(after, indent=2, sort_keys=True)
        assert trial.read_reward() == (0.25, "reward.txt")
        assert (verifier / "reward.txt").read_text(encoding="utf-8") == "0.25\n"
        assert sorted(os.listdir(verifier)) == ["reward.txt"]
        info = os.lstat(verifier)
        assert (info.st_uid, info.st_gid, info.st_mode & 0o7777) == (0, 0, 0o700)
        assert (agent_logs / "tofu-result.json").read_text(encoding="utf-8") == "agent-log\n"
