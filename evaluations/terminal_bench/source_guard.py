"""Benchmark-source blocklist shared by every native Terminal-Bench arm.

Responsibility: one rule for which fetches are Terminal-Bench answer sources
(the benchmark's own repositories and dataset copies, its hidden tests and
reference solutions, and the mirrors the contamination audit found), and its
projection into each harness's own blocking mechanism:

- every arm: the trial egress proxy (``vm_trial.EgressProxy``) refuses
  ``ANSWER_HOSTS`` at CONNECT and any plain-HTTP URL containing a marker;
- Claude Code: a PreToolUse hook on Bash, WebFetch and WebSearch
  (``claude_code_hook_script``);
- OpenCode: ``permission.bash`` deny patterns (``opencode_permission``).
  OpenCode 1.19.0's config takes only one action for ``webfetch`` (no URL
  patterns) and ``run --pure`` loads no plugins, so its web fetches are
  guarded by the proxy alone: answer hosts refused, repository paths audited;
- Pi: a ``tool_call`` extension that blocks matching calls (``pi_extension``);
- Tofu: the product's ``search.skip_domains`` setting (``tofu_server_config``)
  projected by :func:`tofu_skip_domains`. Answer hosts stay host rules;
  source repositories and benchmark names become path patterns on every host
  (``lib/search/url_blocklist.py`` grammar), refused at the fetch gate, in
  search listings, at browser navigation and in ``run_command`` URL transfers
  by the product's pre-tool hook. The trajectory audit remains the backstop
  for non-URL text (scheme-less ``git clone github.com/...``, repository IDs).

HTTPS hides paths from the proxy, so paths on shared hosts (GitHub, Hugging
Face) are refused only where a harness exposes the URL or command before it
runs. Every other use of those hosts stays open: tasks legitimately clone
repositories and download models. Each projection has an in-container canary
(``guard_check_program``) that the trial runs before the agent starts.

Entry points: ``blocked``, the projections above, ``guard_check_program``.
Dependencies: standard library.
"""
from __future__ import annotations

import json
import re
import shlex

# Hosts that serve only Terminal-Bench material (task explorer pages with
# tests, the Harbor registry, the leaderboard): every URL on them and on their
# subdomains is refused.
ANSWER_HOSTS = ("tbench.ai", "marginlab.ai", "harborframework.com")
# Owners and mirror repositories on shared hosts that publish the benchmark's
# tasks, tests or solutions; refused wherever they appear in a fetch or command.
# The last two are mirrors found by the 2026-11-03 trajectory audit.
SOURCE_REPOS = ("harbor-framework", "laude-institute", "bruce-lys/llm-benchmark", "eyan-l/agent_bench-nscc")
# Benchmark names kept by forks and mirror paths (lynl7130/terminal-bench,
# .../benchmarks/terminal_bench/tasks); refused inside a URL only, because a
# task's own files may mention the benchmark.
NAME_MARKERS = ("terminal-bench", "terminal_bench", "terminalbench")
# What makes a token a URL for NAME_MARKERS: a scheme, or a shared code/data host
# reached without one (``git clone github.com/...``, ``curl raw.githubusercontent.com/...``).
URL_PREFIXES = ("://", "github.com", "githubusercontent.com", "huggingface.co", "hf.co")
# Every marker of a benchmark URL (the proxy's plain-HTTP rule and the audit's source calls).
URL_MARKERS = SOURCE_REPOS + NAME_MARKERS
# OpenCode's permission wildcard is case-sensitive: add the spellings the audit saw.
CANONICAL_CASE_REPOS = ("Bruce-lys/LLM-benchmark", "Eyan-L/Agent_Bench-NSCC")

# One answer-host URL and one repository path on a shared host must be refused;
# the control on the same shared host must stay allowed.
CANARY_BLOCKED_URLS = ("https://www.tbench.ai/registry/terminal-bench/2.0",
                       "https://raw.githubusercontent.com/harbor-framework/terminal-bench-2/main/README.md")
CANARY_ALLOWED_URL = "https://raw.githubusercontent.com/python/cpython/main/README.rst"

# ``{match}`` is the refused reference. The text needs no shell or JSON escaping.
BLOCK_MESSAGE = ("Blocked by the evaluation harness: {match} is a Terminal-Bench answer source (the "
                 "benchmark's own repositories, dataset copies, tests or solutions). Solve the task "
                 "without it; other internet access is unaffected.")

GUEST_LOG_DIR = "/logs/agent"
CLAUDE_CODE_HOOK_FILE = "claude-benchmark-guard.sh"
PI_EXTENSION_FILE = "pi-benchmark-guard.mjs"
TOFU_SERVER_CONFIG_FILE = "tofu-server-config.json"
_LITERAL = re.compile(r"^[a-z0-9._/:-]+$")


def _alternation(items) -> str:
    """Literal alternatives valid in Python, JavaScript and POSIX ERE alike (only ``.`` is escaped)."""
    for item in items:
        if not _LITERAL.fullmatch(item):
            raise ValueError(f"unsupported blocklist literal {item!r}")
    return "(" + "|".join(item.replace(".", r"\.") for item in items) + ")"


def _patterns(not_url_char: str) -> tuple[str, str, str]:
    """(answer host, source repo, name inside a URL) patterns over lowercased text.

    ``not_url_char`` is the dialect's class body for characters that end a URL
    token. No look-around, so the same patterns run in bash ``[[ =~ ]]``.
    """
    hosts = "(^|[^a-z0-9-])" + _alternation(ANSWER_HOSTS) + "([^a-z0-9-]|$)"
    repos = _alternation(SOURCE_REPOS)
    names = _alternation(URL_PREFIXES) + f"[^{not_url_char}]*" + _alternation(NAME_MARKERS)
    return hosts, repos, names


_PYTHON_PATTERNS = tuple(re.compile(pattern) for pattern in _patterns(r"\s\"'<>"))


def blocked(text: str) -> str | None:
    """The answer-source reference in a URL, command or tool input, or ``None``."""
    lowered = text.lower()
    for pattern in _PYTHON_PATTERNS:
        match = pattern.search(lowered)
        if match:
            return match.group(0).strip()
    return None


def claude_code_hook_script() -> str:
    """Claude Code PreToolUse hook: exit 2 blocks the tool call and shows stderr to the model.

    Bash builtins only (every task image has bash), so the hook cannot fail on
    a missing tool; ``guard_check_program`` proves it refuses in the task image.
    """
    hosts, repos, names = _patterns("[:space:]\"'<>")
    message = BLOCK_MESSAGE.format(match="${BASH_REMATCH[0]}")
    return "\n".join([
        "#!/usr/bin/env bash",
        "# Terminal-Bench answer-source guard (evaluations/terminal_bench/source_guard.py).",
        "IFS= read -r -d '' payload",
        "text=${payload,,}",
        f"hosts={shlex.quote(hosts)}",
        f"repos={shlex.quote(repos)}",
        f"names={shlex.quote(names)}",
        'if [[ $text =~ $hosts || $text =~ $repos || $text =~ $names ]]; then',
        f'  printf "%s\\n" "{message}" >&2',
        "  exit 2",
        "fi",
        "exit 0",
        "",
    ])


def claude_code_hooks(hook_path: str) -> dict:
    """``hooks`` settings entry running the guard before every network-capable tool."""
    return {"PreToolUse": [{"matcher": "Bash|WebFetch|WebSearch",
                            "hooks": [{"type": "command", "command": f"bash {shlex.quote(hook_path)}"}]}]}


def opencode_permission() -> dict:
    """OpenCode ``permission`` rules: every command allowed except answer-source fetches.

    OpenCode 1.19.0 (config schema ``ConfigPermissionV1``) accepts a pattern
    map for ``bash`` but a single action for ``webfetch``. Its wildcard is a
    case-sensitive glob over the command, and the longer, more specific
    pattern wins over ``"*"``.
    """
    patterns = [f"*{host}*" for host in ANSWER_HOSTS]
    patterns += [f"*{repo}*" for repo in SOURCE_REPOS + CANONICAL_CASE_REPOS]
    patterns += [f"*{prefix}*{name}*" for prefix in URL_PREFIXES for name in NAME_MARKERS]
    return {"bash": {"*": "allow", **{pattern: "deny" for pattern in patterns}}}


def pi_extension() -> str:
    """Pi extension (ES module): a ``tool_call`` handler that blocks answer-source calls."""
    patterns = ", ".join(f"new RegExp({json.dumps(pattern)})" for pattern in _patterns(r"\s\"'<>"))
    return "\n".join([
        "// Terminal-Bench answer-source guard (evaluations/terminal_bench/source_guard.py).",
        f"const PATTERNS = [{patterns}];",
        f"const MESSAGE = {json.dumps(BLOCK_MESSAGE)};",
        "export function blockedBy(input) {",
        "  const text = JSON.stringify(input ?? null).toLowerCase();",
        "  for (const pattern of PATTERNS) {",
        "    const match = pattern.exec(text);",
        "    if (match) return match[0].trim();",
        "  }",
        "  return null;",
        "}",
        "export default function (pi) {",
        '  pi.on("tool_call", async (event) => {',
        "    const match = blockedBy(event.input);",
        '    if (match) return { block: true, reason: MESSAGE.replace("{match}", match) };',
        "    return undefined;",
        "  });",
        "}",
        "",
    ])


def tofu_skip_domains() -> list[str]:
    """Product ``search.skip_domains`` entries refusing what :func:`blocked` refuses in a URL.

    The other arms refuse a source repository or a benchmark name anywhere in
    a URL, on any host and at any depth (mirrors nest the benchmark inside
    other repositories: ``.../tests/__image__/tb21/terminal-bench-2-1/...``).
    The product grammar (``lib/search/url_blocklist.py``) states that as
    ``*/**/*marker*``: every host, any number of segments, a segment
    containing the marker. Answer hosts stay host rules, plus path rules for
    proxies that carry the answer host's URL in their own path.
    """
    entries = set(ANSWER_HOSTS)
    for host in ANSWER_HOSTS:
        entries.update((f"*/**/{host}", f"*/**/*.{host}"))
    # The product matcher lower-cases URLs and entries alike, so the
    # canonical-case mirror spellings need no entry of their own.
    for literal in SOURCE_REPOS + NAME_MARKERS:
        owner, _, name = literal.partition("/")
        entries.add(f"*/**/*{owner}/{name}*" if name else f"*/**/*{literal}*")
    return sorted(entries)


def tofu_server_config() -> dict:
    return {"search": {"skip_domains": tofu_skip_domains()}}


def tofu_config_install(source: str) -> str:
    """Shell step placing the server config where headless Tofu reads it.

    The Tofu runtime ships no in-tree ``data/``, so ``runtime_path_policy``
    resolves the data root to ``${XDG_DATA_HOME:-$HOME/.local/share}/Tofu``.
    """
    directory = '"${XDG_DATA_HOME:-$HOME/.local/share}/Tofu/data/config"'
    return f"mkdir -p {directory} && cp {shlex.quote(source)} {directory}/server_config.json"


def _proxy_canary() -> str:
    """Container-side CONNECT to an answer host through the trial proxy; recorded, not fatal.

    Uses bash ``/dev/tcp`` because images need not ship curl, inside a subshell
    so a failed redirection cannot end the check. The proxy's unit tests own
    its correctness; this line shows the container routes through it.
    """
    target = shlex.quote(CANARY_BLOCKED_URLS[0].split("/")[2] + ":443")
    return ("( proxy=${https_proxy:-${HTTPS_PROXY:-}}; proxy=${proxy#*://}; proxy=${proxy%%/*}; "
            'if exec 3<>"/dev/tcp/${proxy%:*}/${proxy##*:}"; then '
            f'printf "CONNECT %s HTTP/1.1\\r\\nHost: %s\\r\\n\\r\\n" {target} {target} >&3; '
            "IFS= read -r -t 30 reply <&3; reply=${reply//$'\\r'/}; "
            'echo "proxy-canary CONNECT ' + CANARY_BLOCKED_URLS[0].split("/")[2] + ' -> ${reply:-<no reply>}"; '
            'else echo "proxy-canary: cannot connect to ${proxy:-<unset>}"; fi ) 2>&1')


def guard_check_program(arm: str, *, tofu_python: str = "", opencode_binary: str = "",
                        pi_node: str = "", log_dir: str = GUEST_LOG_DIR) -> str:
    """In-container canary run after install: the arm's own guard refuses the canary URLs.

    Exits 0 only when every canary is refused and the control URL is allowed,
    so a trial never starts with a broken guard.
    """
    lines = ["fail=0", _proxy_canary()]
    log = log_dir
    if arm == "claude-code":
        hook = shlex.quote(f"{log}/{CLAUDE_CODE_HOOK_FILE}")
        cases = ([("WebFetch", "url", url, 2) for url in CANARY_BLOCKED_URLS]
                 + [("Bash", "command", f"curl -sSL {url}", 2) for url in CANARY_BLOCKED_URLS]
                 + [("WebFetch", "url", CANARY_ALLOWED_URL, 0), ("Bash", "command", f"curl -sSL {CANARY_ALLOWED_URL}", 0)])
        for tool, key, value, expected in cases:
            payload = shlex.quote(json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                                              "tool_input": {key: value}}))
            lines.append(f'printf "%s" {payload} | bash {hook} 2>/dev/null; code=$?; '
                         f'echo "guard-canary {tool} {shlex.quote(value)} -> exit $code (want {expected})"; '
                         f'[ "$code" -eq {expected} ] || fail=1')
    elif arm == "pi":
        cases = [[f"curl -sSL {url}", True] for url in CANARY_BLOCKED_URLS] + [[f"curl -sSL {CANARY_ALLOWED_URL}", False]]
        script = ("const m = await import(" + json.dumps(f"file://{log}/{PI_EXTENSION_FILE}") + "); let handler; "
                  "m.default({on: (name, fn) => { if (name === 'tool_call') handler = fn; }}); "
                  "let ok = Boolean(handler); for (const [command, want] of " + json.dumps(cases) + ") { "
                  "const reply = await handler({toolName: 'bash', input: {command}}); "
                  "const got = Boolean(reply && reply.block); "
                  "console.log('guard-canary bash', command, '-> block', got, '(want', want + ')'); "
                  "if (got !== want) ok = false; } process.exit(ok ? 0 : 1);")
        lines.append(f"{shlex.quote(pi_node)} --input-type=module -e {shlex.quote(script)} || fail=1")
    elif arm == "opencode":
        lines.append(f'resolved=$({shlex.quote(opencode_binary)} debug config 2>&1) || fail=1')
        for pattern in opencode_permission()["bash"]:
            if pattern != "*":
                lines.append(f'[[ $resolved == *{shlex.quote(json.dumps(pattern))}* ]] || '
                             f'{{ echo "guard-canary opencode lacks rule {pattern}"; fail=1; }}')
        lines.append('echo "guard-canary opencode resolved permission rules (fail=$fail)"')
    elif arm == "tofu":
        # Every canary (both answer hosts AND repository paths on shared
        # hosts) must be refused by the product's own URL blocklist; the
        # control URL on the same shared host must stay reachable, and the
        # search listing must drop a blocked result's URL/snippet as well.
        script = ("import lib; from lib.search.search_bridge import sync_search_config; "
                  "from tofu_search.fetch.utils import _should_fetch; "
                  "from lib.search.url_access import blocked_text_message; sync_search_config(); "
                  f"missing = [h for h in {list(ANSWER_HOSTS)!r} if h not in lib.SKIP_DOMAINS]; "
                  f"refused = {{u: not _should_fetch(u) for u in {list(CANARY_BLOCKED_URLS)!r}}}; "
                  f"allowed = _should_fetch({CANARY_ALLOWED_URL!r}); "
                  f"shell = blocked_text_message('curl -sSL {CANARY_BLOCKED_URLS[1]}'); "
                  f"shell_ok = blocked_text_message('curl -sSL {CANARY_ALLOWED_URL}'); "
                  "print('guard-canary tofu skip_domains missing', missing, 'refused', refused, "
                  "      'allowed', allowed, 'shell-block', bool(shell), 'shell-allow', bool(shell_ok)); "
                  "raise SystemExit(0 if not missing and all(refused.values()) and allowed "
                  "                 and shell and not shell_ok else 1)")
        config = shlex.quote(f"{log}/{TOFU_SERVER_CONFIG_FILE}")
        lines += ['scratch=$(mktemp -d)',
                  f'mkdir -p "$scratch/Tofu/data/config" && cp {config} "$scratch/Tofu/data/config/server_config.json" '
                  f'&& XDG_DATA_HOME="$scratch" {shlex.quote(tofu_python)} -I -c {shlex.quote(script)} || fail=1',
                  'rm -rf "$scratch"']
    else:
        raise ValueError(f"no benchmark-source guard for arm {arm!r}")
    lines.append('exit "$fail"')
    return "\n".join(lines) + "\n"
