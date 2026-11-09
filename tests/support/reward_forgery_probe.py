"""Attack the Terminal-Bench reward channel as another uid (stdlib only).

The real container attacker is a process the agent or ``tests/test.sh`` drops
to a de-privileged uid (65534). Only the mount's own DAC rules decide the
outcome, so attacking the bind source on the host and ``/logs/verifier`` in
the container are the same check; this module drives both.

Used by ``tests/evaluations/test_tb_reward_forgery.py`` and by the root-in-VM
smoke (this module is uploaded verbatim and imported without the repository).
Nothing here is scored: ``forged`` is every attack's payload, so a reward that
ever reads back as ``forged`` instead of the legitimate value is the forgery.
"""
import argparse
import json
import os
import stat
import sys
import time

FORGED = "forged"


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def outcome(action):
    """``blocked`` with the OS error, or ``forged`` with what the attack achieved."""
    try:
        return action()
    except OSError as error:
        return {"result": "blocked", "detail": "%s: %s" % (type(error).__name__, error)}
    except Exception as error:  # a forged symlink making the read unparseable is not a block
        return {"result": "forged", "detail": "%s: %s" % (type(error).__name__, error)}


def attack(verifier, outside, *, wait_for_reward=False):
    """Every way this process could plant, replace or destroy the reward; never mutates ``outside``.

    ``verifier`` is the reward directory, ``outside`` a writable directory
    elsewhere on the same volume. Each case reports ``blocked`` only when the
    operating system refused the attack itself, so a successful delete or
    rename counts as a forgery even when the next case finds no reward file.
    """
    verifier, outside = os.path.abspath(verifier), os.path.abspath(outside)
    reward = os.path.join(verifier, "reward.txt")
    forged_outside = os.path.join(outside, "forged.txt")
    write_text(forged_outside, FORGED + "\n")
    if wait_for_reward:
        for _ in range(1200):
            try:
                exists = os.path.exists(reward)
            except OSError:
                exists = True
            if exists:
                break
            time.sleep(0.05)
    results = {"actual_uid": os.geteuid(), "actual_gid": os.getegid()}

    def record(name, action):
        results[name] = outcome(action)

    def create():
        write_text(reward, FORGED + "\n")
        return {"result": "forged", "detail": "created reward.txt"}

    def modify():
        with open(reward, "r+", encoding="utf-8") as handle:
            handle.seek(0)
            handle.write(FORGED + "\n")
            handle.truncate()
        return {"result": "forged", "detail": "rewrote reward.txt in place"}

    def truncate_write():
        write_text(reward, FORGED + "\n")
        return {"result": "forged", "detail": "truncated and rewrote reward.txt"}

    def rename_swap():
        staged = os.path.join(verifier, ".forged-swap.txt")
        try:
            write_text(staged, FORGED + "\n")
        except OSError as error:
            # Never let a failed staging write stand in for the swap itself.
            return {"result": "blocked", "detail": "staging: %s" % error}
        os.replace(staged, reward)
        return {"result": "forged", "detail": "renamed .forged-swap.txt over reward.txt"}

    def symlink_swap():
        staged = os.path.join(verifier, ".forged-swap-link")
        try:
            os.symlink(os.path.relpath(forged_outside, verifier), staged)
        except OSError as error:
            return {"result": "blocked", "detail": "staging: %s" % error}
        os.replace(staged, reward)
        return {"result": "forged", "detail": "renamed a symlink over reward.txt"}

    def hardlink_swap():
        staged = os.path.join(verifier, ".forged-swap-hardlink")
        try:
            os.link(forged_outside, staged)
        except OSError as error:
            return {"result": "blocked", "detail": "staging: %s" % error}
        os.replace(staged, reward)
        return {"result": "forged", "detail": "renamed a hard link over reward.txt"}

    def delete():
        os.unlink(reward)
        return {"result": "forged", "detail": "deleted reward.txt"}

    def rename_dir():
        os.rename(verifier, verifier + ".moved")
        return {"result": "forged", "detail": "renamed the verifier directory itself"}

    def chmod_dir():
        os.chmod(verifier, 0o777)
        return {"result": "forged", "detail": "chmod 0777 on the verifier directory"}

    def read_reward():
        with open(reward, "r", encoding="utf-8") as handle:
            return {"result": "forged", "detail": "read reward: " + handle.read().strip()[:80]}

    record("create_reward", create)
    record("modify_reward_in_place", modify)
    record("truncate_reward_in_place", truncate_write)
    record("rename_over_reward", rename_swap)
    record("symlink_over_reward", symlink_swap)
    record("hardlink_over_reward", hardlink_swap)
    record("delete_reward", delete)
    record("rename_verifier_dir", rename_dir)
    record("chmod_verifier_dir", chmod_dir)
    record("read_reward", read_reward)
    try:
        info = os.lstat(verifier)
        results["verifier_dir"] = {"uid": info.st_uid, "gid": info.st_gid, "mode": oct(stat.S_IMODE(info.st_mode)),
                                   "entries": sorted(os.listdir(verifier))}
    except OSError as error:
        results["verifier_dir"] = {"error": "%s: %s" % (type(error).__name__, error)}
    return results


def forgeries(results):
    """Attack names whose result was not ``blocked`` (``verifier_dir`` is observation only)."""
    return sorted(name for name, value in results.items()
                  if isinstance(value, dict) and value.get("result") == "forged")


def run_as_uid(function, uid, gid, *args, **kwargs):
    """Fork a child that drops to ``uid``/``gid`` and returns ``function(...)``; requires real root."""
    if os.geteuid() != 0:
        raise PermissionError("dropping to another uid requires real root, not a single-uid user namespace")
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        status = 1
        try:
            os.close(reader)
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
            payload = json.dumps(function(*args, **kwargs), sort_keys=True).encode()
            while payload:
                written = os.write(writer, payload)
                payload = payload[written:]
            status = 0
        except BaseException:
            import traceback
            os.write(sys.stderr.fileno(), traceback.format_exc().encode())
        finally:
            os._exit(status)
    os.close(writer)
    chunks = []
    while True:
        chunk = os.read(reader, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(reader)
    _, status = os.waitpid(pid, 0)
    if not chunks:
        raise RuntimeError("probe child produced no result (status %d)" % status)
    return json.loads(b"".join(chunks).decode())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verifier")
    parser.add_argument("outside")
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int, default=65534)
    parser.add_argument("--wait-for-reward", action="store_true")
    args = parser.parse_args(argv)
    if args.uid is None:
        results = attack(args.verifier, args.outside, wait_for_reward=args.wait_for_reward)
    else:
        results = run_as_uid(attack, args.uid, args.gid, args.verifier, args.outside,
                             wait_for_reward=args.wait_for_reward)
    print(json.dumps(results, indent=2, sort_keys=True))
    return 1 if forgeries(results) else 0


if __name__ == "__main__":
    sys.exit(main())
