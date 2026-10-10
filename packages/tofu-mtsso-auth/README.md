# Official MTSSO adapter for internal MCP servers

The canonical adapter to the official Skill `mtsso-skills-official` V13
(FRIDAY skill 6556) and `@mtfe/mtsso-auth-official@1.0.11`.
It invokes the installed `mtsso-moa-local-exchange` executable; it does not
implement grants, bundle credentials, or install npm packages during a tool call.

Install the official npm package from the internal registry using your approved
Node environment, then install this Python package in the MCP environment:

```sh
npm install --global --prefix "$HOME/.local" --registry https://r.npm.internal.example.com @mtfe/mtsso-auth-official@1.0.11
pip install -e packages/tofu-mtsso-auth
```

Set `MTSSO_AUTH_MODE=official` in each MCP's environment after the official Agent
SSO setup is present. `auto` (default) selects the official provider only when
an official Agent configuration is declared; installing its executable alone
retains each MCP's existing authentication provider and cached session. Local
discovery covers `AGENT_SSO_CLIENT_ID`/`AGENT_SSO_CLIENT_SECRET`,
`MTSSO_AGENT_CONFIG_PATH`, `MTSSO_GATEWAY_INTERCEPT_FEATURE`, official
`agent_info` locations (including `MTSSO_WORKDIR`), and
`~/.mtsso/agents/.env`'s `DEFAULT_MTSSO_AGENT_CONFIG_PATH`. A declaration may be
incomplete: it still selects official and fails closed, never borrowing another
application's credentials. Explicit `official` and `legacy` remain authoritative.
Once auto selects official, it stays selected until explicit session clear;
endpoint token invalidation preserves that authority. Status checks never spawn
a command, access the network or start a login. The extended fallback read admits
only regular files and at most 64 KiB; values are never evaluated or copied. `MTSSO_OFFICIAL_EXECUTABLE` can point
to the installed official executable. The CLI owns Agent credential discovery
and local/extended-Agent routing. This adapter passes `--env PROD|TEST`
explicitly and disables CLI self-update with `--auto_update false`, with no
credentials in argv. Updates belong to deployment, not user tool calls. Tofu injects the Python adapter into
all three isolated launch environments; standalone MCP users install it once.

For the official Node shebang, the adapter probes installed `node --version`
commands on PATH within a five-second total budget (two seconds per candidate),
skips unusable runtimes, and launches the same official script with the first
working Node. Set `MTSSO_NODE_EXECUTABLE` to select a specific runtime; an
unusable explicit selection fails closed. No runtime is downloaded by the
adapter. A loader failure becomes `SSO_NODE_RUNTIME_INCOMPATIBLE`, rather than
a login error. CLI stderr remains bounded in memory: only structured public
errors or recognized loader-failure classifications leave the adapter; arbitrary
stderr and credentials are withheld. Recognized missing `client_id` or
`client_secret` errors become `SSO_AGENT_CONFIGURATION_MISSING`; invalid Agent
files become `SSO_AGENT_CONFIGURATION_INVALID`. These fixed messages never echo
paths or credentials. Structured permission/interaction errors take precedence,
even when stdout contains a ticket and stderr carries the actual error. Human
guidance redacts sensitive fields from both streams, including nested arrays.
Unknown failures include the exit code. Error results carry `model_action` and
`recovery_action`: models stop and report, rather than guessing login or retries.


A failed official exchange never falls through to a different credential or
CIBA flow. Exit 42 and the four equivalent older-version error codes preserve
the official human-action message; MOA rejection/cooldown/pending stops retries.
Other output is withheld because it can include tickets or secrets. Successful
JSON requires a nonempty access_token, regardless of exit-zero status. Gateway
placeholders require a verified gateway transport; the current direct HTTP
MCP adapters fail with SSO_GATEWAY_REQUIRED instead of forwarding a placeholder.

Tokens live only in the MCP process session and are cleared on shutdown/logout.
Cache scope is explicit owner + environment + ticket type + exact sorted audience
set. Expiry is bounded to min(expires_in minus 60 seconds, two hours); unknown
expiry is never cached. Cache size is 64 entries (at most 4 MiB of ticket text).
Terminal failures are coalesced for 30 seconds, so concurrent tool calls do not
repeat a denied or pending authorization, including `force_refresh` requests. Cache clear fences in-flight exchanges
from repopulating an old session. Unrelated cache hits never wait on a slow exchange.
CLI stdout/stderr are each limited to 64 KiB, with a 30-second process deadline;
terminal paths kill the subprocess group and reap the process. No CLI starts
at import time or in legacy mode. Cached status is not identity verification:
`whoami` never claims a subject from an unrelated legacy credential.

Problem: merely installing the official CLI replaced working MCP sessions even
when no Agent identity was registered; its missing-client error became a generic
login/MOA hint. This imposed configuration diagnosis and repeated login on the
model. Decision: discover official configuration locally before selecting a
provider, preserve declared authority through failures and ticket invalidation,
and project fixed recovery instructions at the shared MCP boundary. Alternatives:
using executable presence confuses installation with configuration; online probes
on status checks add network/subprocess latency; credential borrowing and fallback
after permission denial can change the acting identity. The CLI still owns all
grants, routing and credential validation.

Problem: MCP tools leak provider-specific failures to the model and retry
login for permission/configuration failures. Decision: let the official CLI own
authentication, project terminal structured errors once, and use a single
bounded safe replay after endpoint auth rejection. Alternatives: reimplementing
MOA/SSO duplicates the official authority; unconditional legacy fallback can
bypass a permission denial; treating gateway placeholders as bearer tickets
pretends a transport integration exists. A PATH-selected Conda Node can also
require a newer GLIBC than the host supplies. Probing installed Node candidates
fixes executable selection without changing the authentication provider;
blindly retrying login or copying raw stderr would obscure the cause or leak
credentials.

Verification: `pytest packages/tofu-mtsso-auth/tests`; internal MCP auth/server
suites cover native HTTP auth typing and provider routing. Live official login
requires an adapted Agent environment, so offline tests do not claim that live
login succeeded.

Measured on the development host (2026-10-06): 10,000 warm cache reads averaged
3.01 microseconds per call, with no CLI spawn; maximum retained ticket text is
4,194,304 bytes. Cold calls are bounded by a five-second runtime probe budget plus the
30-second CLI deadline; single-flight waiters include both budgets. This is a local
adapter microbenchmark, not a measurement of live SSO latency.

Read-only operational diagnosis uses the same installed adapter, without asking a
model to interpret stderr or inspect credentials:

```sh
python -m tofu_mtsso_auth          # local selection only
python -m tofu_mtsso_auth --probe  # explicit bounded official capability check
```

These reports describe provider selection and capability, not authenticated
identity or permission to access a document. The optional probe uses only the
installed official command, with a one-second provider timeout and five-second
process budget after Node selection; it never installs packages.

Measured on the development host (2026-10-08): 10,000 auto selections with no
registered Agent averaged 15.1 microseconds, with no subprocess/network work.
A live forced-official missing-client reproduction returned the typed error in
0.085 seconds with CLI self-update disabled; this is local failure latency,
not a measurement of successful SSO exchange. Discovery adds no resident queue
or credential cache, and admits at most 65,536 fallback-file bytes per call.
