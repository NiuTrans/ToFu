"""Shared SSO adapter command line.

``python -m tofu_mtsso_auth [--probe]``
    Secret-free provider diagnosis. Status never exchanges; probing is explicit.

``python -m tofu_mtsso_auth exec --owner OWNER --ticket ENV=AUDIENCE[,AUDIENCE...] [...] -- COMMAND [ARG...]``
    Mint each declared same-user ticket through ``user_token`` and replace this
    process with COMMAND. A ticket reaches only COMMAND's environment, never
    argv, stdout, stderr, logs or files. ENV must name a credential slot
    (contain TOKEN, TICKET or SSOID) so a runtime-interpreted variable such as
    NODE_OPTIONS or LD_PRELOAD can never echo a ticket into diagnostics.
    Launching is a business call and self-heals like one: a shared session
    that needs login requests one 大象 approval and waits for it. A failure
    prints the adapter envelope on stderr, exits 42 when the user must act
    (official convention) or 1 otherwise, and COMMAND does not run.
"""
import argparse
import json
import os
import re
import sys

from . import OfficialAuthError, provider_status, user_token

_TICKET_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_CREDENTIAL_SLOT = re.compile(r"TOKEN|TICKET|SSOID")


def _status(argv):
    parser = argparse.ArgumentParser(prog="python -m tofu_mtsso_auth",
                                     description="Read-only SSO provider diagnosis.")
    parser.add_argument("--probe", action="store_true", help="Explicitly probe the installed official SSO capability")
    args = parser.parse_args(argv)
    result = provider_status(probe=args.probe)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def _ticket(value):
    name, separator, audiences = value.partition("=")
    audiences = audiences.split(",")
    if (not separator or not _TICKET_ENV.fullmatch(name) or not _CREDENTIAL_SLOT.search(name)
            or not all(audiences)):
        raise argparse.ArgumentTypeError(
            "expected ENV=AUDIENCE[,AUDIENCE...]; ENV is upper-case and contains TOKEN, TICKET or SSOID")
    return name, audiences


def _exec(argv):
    parser = argparse.ArgumentParser(
        prog="python -m tofu_mtsso_auth exec",
        usage="%(prog)s --owner OWNER --ticket ENV=AUDIENCE[,AUDIENCE...] [--ticket ...] "
              "[--environment {PROD,TEST}] -- COMMAND [ARG...]",
        description="Run COMMAND with fresh same-user SSO tickets in its environment only.")
    parser.add_argument("--owner", required=True,
                        help="Launching Tofu owner, e.g. tofu:1; partitions the ticket scope")
    parser.add_argument("--ticket", action="append", required=True, type=_ticket,
                        metavar="ENV=AUDIENCE[,AUDIENCE...]")
    parser.add_argument("--environment", choices=("PROD", "TEST"))
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    if not command:
        parser.error("COMMAND is required after --")
    names = [name for name, _ in args.ticket]
    if len(set(names)) != len(names):
        parser.error("declare each ticket variable once")
    child = dict(os.environ)
    try:
        for name, audiences in args.ticket:
            child[name] = user_token(args.owner, audiences, environment=args.environment)
    except OfficialAuthError as error:
        envelope = error.envelope()
        print(json.dumps(envelope, ensure_ascii=False), file=sys.stderr)
        return 42 if envelope["user_action_required"] else 1
    try:
        os.execvpe(command[0], command, child)
    except OSError as error:
        print(json.dumps({"ok": False, "error_code": "COMMAND_NOT_STARTED",
                          "error": "%s: %s" % (command[0], error.strerror or error)},
                         ensure_ascii=False), file=sys.stderr)
        return 127


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["exec"]:
        return _exec(argv[1:])
    return _status(argv)


if __name__ == "__main__":
    raise SystemExit(main())
