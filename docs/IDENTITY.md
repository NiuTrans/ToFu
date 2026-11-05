# Identity and credential authority

This document is the entry point for authentication subjects, repository
ownership, and bearer credentials. The field-level authority is
[`contracts/identity_v1.yaml`](../contracts/identity_v1.yaml).

## Three identifiers, three jobs

| Identifier | Shape | Used for | Never used for |
|---|---|---|---|
| `account_user_id` | opaque string such as `usr_*` | login, account administration, billing | repository ownership |
| `owner_user_id` | positive integer | every user-owned repository, task, event, cache, and device registry | display or password lookup |
| `tenant_id` | optional opaque string | future enterprise partition boundary | an implicit process global |

Personal mode reserves owner `1`. Each tenant account receives a different
numeric owner from a transactionally locked Sidecar sequence. An account ID is
never parsed into a number and an owner ID is never exposed as the account
subject.

### Exact owner normalization

Problem: generic `int(value)` silently truncated fractional IDs and accepted
floating-point, byte and arbitrary numeric objects. A custom integer conversion
could also change the value represented by an integer or text wrapper. These
coercions can select a different owner instead of rejecting malformed identity.

Decision: `lib.identity.require_user_id` is the common normalization boundary.
It accepts integers and the existing base-10 integer-text syntax; booleans,
floats (including integral-valued ones), byte containers and other objects fail
with the existing contextual `ValueError`. Integer subclasses use their exact
integer representation; text is parsed with an explicit decimal base, without
calling custom `__int__` conversions. Positivity is checked after parsing.
No size cap or storage-specific integer range is introduced here.

Principal creation/restoration, authentication contexts, request binding and
services keep using this owner. No persisted integer ID or valid textual ID
requires migration; previously tolerated non-identity representations are now
rejected. Alternatives rejected: testing only for a fractional part still admits
floating-point IDs whose precision may already be lost; validating independently
in each caller permits drift; parsing through a float changes large exact IDs.
Verify `tests/identity/test_owner_id_boundary.py` and the neighboring request,
task, credential, repository and native ownership contracts.

## Request flow

```text
Bearer/cookie/device credential
  -> select one transport candidate
  -> auth_credentials read-current validation + conditional audit touch
  -> AuthContext(key, owner, account, tenant, scopes)
  -> PrincipalContext(subject=key, owner, tenant, scopes)
  -> owner-scoped service/repository operation
```

`routes/api_v1/auth.py` is the only HTTP authentication boundary. Missing or
invalid ownership is denied there. Lower layers receive `PrincipalContext` or
an explicit positive owner and never consult a current-user global.

The account field may be empty for service credentials. When it is present,
authentication succeeds only while that account exists, is active, and maps
to the credential's owner. Suspending an account therefore invalidates all of
its bound credentials without a cache-invalidation race.

## Credential lifecycle

`auth_credentials` in the selected storage authority is the sole credential store. Creating a
credential returns plaintext once and persists only its SHA-256 digest.
List/get operations never return the digest. List, get, update, and revoke all
require the exact owner and tenant boundary.

Authentication reads current authority on every request; `last_used_at` is a
separate conditional maintenance command. Audit-touch failure cannot grant
authority or deny an already validated request. Disabled,
expired, revoked, unknown, and suspended-account credentials fail closed.
Admin privilege is a credential tier: updating scopes cannot promote a live
credential to admin or demote an admin credential accidentally.

### Credential transport and request resolution

Problem: HTTP and WebSocket maintained separate precedence rules, diagnostic
source detection reparsed the same headers, and an open-mode refusal could
validate its token again while falling through to the credential-required gate.

Decision: `lib/api_keys/transport.py` selects one untrusted candidate and source
from supplied transport values. Explicit Bearer wins, even with an unrecognized
prefix; only the existing Tofu prefixes are eligible in x-api-key, session cookie
and query transports. HTTP supplies the optional query transport for first-link
login. WebSockets omit it. Neither selection nor a candidate grants authority.

HTTP retains this immutable candidate only in its request context, including
for mobile download-link construction, and validates it once through current
storage. Open-mode refusal uses the same result in the credential-required gate.
The next request validates again, so revocation and account suspension stay
current. Cross-site/peer policy, device-only credentials, cookie redirects,
scope checks and WebSocket cancellation/joining retain their existing owners.

Alternatives rejected: copying a parser into each transport invites drift;
retrying another credential after an explicit invalid Bearer changes identity;
caching authentication across requests delays revocation. A shared middleware
state machine would conflate HTTP responses with socket admission and teardown.
No authentication cache, additional mode, retry or timing control is introduced.

Verify credential-transport parity, open-mode refusal and revocation, first-link
cookies, mobile links, device credential gates, socket admission/cancellation,
and the storage credential lifecycle. The current-read/audit-touch contract is
defined in `identity_v1.yaml`; deployment details live in
[`PROXY_RUNTIME.md`](PROXY_RUNTIME.md#credential-writer-budget).

The first personal credential uses an atomic create-if-empty command. The
mode-0600 `.first_run_token` file is only a one-time recovery copy of that
plaintext; deleting or corrupting it cannot alter credential authority.

## Device bridge

Remote browser and desktop agents use an owner-scoped credential with the
literal `agents:bridge` scope. There is no deployment-wide bridge secret,
credential-free LAN mode, or IP-address trust. The packaged desktop app uses a
non-persisted process capability for its in-process agent; that capability is
accepted only by `/api/desktop/poll` and always maps to personal owner `1`.

Extension downloads and agent installers mint a fresh device credential. If
the credential authority is unavailable, download returns 503. Shipping a
package known to be unable to connect is not a valid degraded mode.

## Borrowing an existing Codex login

Problem: independently refreshing a copied Codex refresh token can race the
CLI and invalidate both sessions. Decision: the native Codex login remains the
credential and refresh authority; Tofu borrows only a currently valid access
token. Alternative rejected: importing `auth.json` into an independently
refreshing Tofu OAuth store. See [Codex authentication](https://learn.chatgpt.com/docs/auth).

`lib/oauth/codex_command_login.py` automatically starts the installed system
Codex through the OS user's interactive shell on POSIX, then obtains the actual
`codexHome` from app-server `initialize` and managed restrictions through
`config/read` / `configRequirements/read`. It does not require users to set an
environment variable or scan other users' credentials. An unavailable native
command cannot silently select an unrelated stale default login.

Problem: background launchers can supply `SHELL=/bin/sh` and a minimal PATH,
selecting an obsolete or broken Codex ahead of the user's working installation.
Decision: resolve the current OS account's registered shell first, using the
environment shell only when no supported account shell is available. Run its
interactive setup and include that selection in metadata-cache provenance.
An explicit `CODEX_HOME` still wins after shell setup; deployments launched
outside a Codex session must pass that setting when their intended native login
uses a non-default home. Alternatives rejected: hardcoding installation paths,
scanning alternative credential directories, or borrowing a stale default login
after a native-command failure.

Problem: server migration can leave an explicit `CODEX_HOME` pointing at a
missing directory even though the installed CLI is runnable. Decision: on the
POSIX native adapter, stat that exact explicit home before metadata spawn or
reuse and immediately before publishing successful metadata or failures, without
creating it. Startup, pipe and local metadata-stamp operations cannot rely on an
earlier availability snapshot. The same final publication check covers warm
metadata/grace after stamp reads and every native-renewal fast return, including
an already-valid file bearer. Source loss withdraws only environment-matching
cache/session authority under the existing bounded auth flight. Healthy reads
remain lock-free; a busy flight cannot authorize grace for an unavailable home.
Confirmed absence, a dangling directory symlink, or a non-directory returns the
redacted, transient `codex_home_unavailable`; permission and unknown filesystem
failures remain `codex_command_unavailable`, never evidence of absence. A missing
home cannot authorize verified-metadata reuse, credential renewal, a default or
another account's home, desktop discovery, or sign-in. The bound source and
ProviderAccess survive; restore the intended existing managed-login directory or
correct the serving process's environment, then retry. An empty `CODEX_HOME=`
(the `.env` way to clear a stale launch-shell value) is unset, never a missing
home. Because only an explicit override can produce this issue, it is always an
operator misconfiguration or outage, never an expected absence: summary and
turn preparation keep their bounded transient retry and warnings for it.
Alternative rejected: suppressing them would hide an actionable outage that a
remount or restored directory can still clear within the retry window.
Restored directories and changed launcher
environments invalidate the short home-failure cache; explicit recovery also
rechecks it. A late missing/inaccessible source revokes verified grace and does
not retain a failed-probe TTL that delays restoration. Positive ENOSPC and native
policy/source rejection keep their original attribution; immediately before
alternate-source discovery, confirmed explicit-home absence independently refuses
desktop borrowing even when another cause is preserved. Native identity/configuration
validation and storage-full attribution remain separate; the Windows file-only seam
is unchanged. Tests: `tests/oauth/test_codex_home_migration.py`.
Assumption: an explicit home denotes an operator-provisioned native login source,
not permission for Tofu to initialize a replacement. Remove this preflight only
when native Codex supplies equally distinct, non-mutating home availability
errors before metadata startup and cache reuse preserves the same source checks.
Alternative rejected: mkdir or searching another directory would conceal a
migration fault and could silently change the credential authority.

Metadata revalidation reuses one serialized native app-server session instead
of rerunning the interactive shell and initialize on every cache expiry. It
re-reads native config and requirements; it never requests account renewal.
Launcher-environment provenance is hashed, and known shell/config/executable
stamps invalidate reuse. Sessions rotate at demand after 90 seconds and expire
absolutely at 120 seconds because arbitrary shell hooks and remote requirements
may be loaded only at native startup. Warm reads cannot extend that original
policy-age limit. Source changes and native policy rejection fail closed;
renewal remains a separately reaped one-shot native exchange after live account
validation. Clear/exit cancel an in-flight native wait, then drain the
transport, reader and idle reaper.

Problem: a healthy native startup can be slow. On the development host (Codex
0.160.0) app-server `initialize` took 8.9–9.3 seconds with `CODEX_HOME` on
network storage holding large native state, against 0.32 seconds on local
disk, while `codex --version` (0.10 seconds) and `bash -ic true` (0.06
seconds) stayed fast. The former fixed 4-second cold exchange therefore failed
every discovery and renewal (`phase=initialize issue=codex_command_timeout
elapsed_ms=3997`) and the borrowed access token stayed expired. Decision: spawn plus `initialize` of a new
app-server (first use, rotation, source change, one-shot renewal) has its own
cold startup budget; each exchange on an initialized app-server keeps 4 seconds.
The cold budget is learned per hashed launcher provenance, process-locally for
at most 16 provenances: it starts at 4 seconds, doubles after an `initialize`
timeout that used the whole budget, becomes the larger of twice the observed
startup and three quarters of the previous budget after a completed one, and
stays within 4 seconds and a 30-second ceiling
(`TOFU_CODEX_NATIVE_STARTUP_MAX_SECONDS`, accepted from 4 to 300 seconds;
invalid values warn once and keep 30). Discovery, the demand revalidation
worker and renewal use it whenever native Codex may start, including the lock
wait behind another flight; one credential read shares a single such flight
between discovery and renewal. Healthy warm reads and verified-metadata reads
stay lock-free. Learned budgets are timing hints, not credential authority;
clear, logout and reconnect keep them. An `initialize` timeout that used the
whole budget at the ceiling refuses that launcher's cold starts for 60 seconds
(discovery caches that failure as long); clear, logout, reconnect or a changed
ceiling end the window. Every source, storage, policy, single-flight,
teardown and policy-age guarantee above is unchanged. Alternatives rejected:
raising the fixed constant would lengthen every warm exchange and still fail on
slower storage; moving `CODEX_HOME` to local disk breaks host migration, where
the explicit home is the operator's authoritative source.

Problem: a learned budget alone left four latency hazards. A flight can hold
the lock for the ceiling plus one exchange (34 seconds by default), above the
default 30-second shutdown deadline, while logout, reconnect or exit waits to
clear. A refresh read chained discovery and renewal into two flights. One fast
warm-cache start reset a slow home's budget to 4 seconds. A start slower than
the ceiling was re-paid by every recovery and renewal. Decision: clear
cancels in-flight waits, and each flight's owner reaps its own child and
publishes nothing. One read shares one flight. Fast starts decay the budget by
at most a quarter, and an exhausted ceiling opens the refusal window above.
Alternatives rejected: a timed lock in clear would abandon children their
flights still own. Halving on a fast start leaves no headroom. A longer
discovery failure cache alone leaves recovery and renewal re-paying the
ceiling. Bounds and host measurements live in
[RESOURCE_CONTROL.md](RESOURCE_CONTROL.md#native-codex-cold-startup-budget).

`codex_local_login.py` reads an access-only snapshot from that native-selected
home, bounded to 1 MiB. Command-confirmed symlink files resolve to their real
owned regular targets; other-user ownership, writable files, malformed JWT
expiry and account changes fail closed. An absent Codex command (including the
current Windows file-only adapter) retains the old strict file seam. The fixed
upstream validates the bearer; JWT metadata is not local authorization.

Native launch checks independently known free bytes and inodes for the OS and
selected Codex home. `codex_home_storage_full` is transient, keeps the bounded
verified-metadata fallback and does not revoke login. An ENOSPC spawn/early-exit
failure receives the same issue. Task and settings feedback instructs releasing
space or inodes and retrying, without starting a new sign-in. Unknown inode
accounting alone never skips the known byte dimension.

Expired file-backed login may request native `account/read(refreshToken=true)`
during reconciliation or explicit recovery. Only Codex refreshes or writes its
credentials; Tofu never copies refresh tokens, initiates sign-in or signs out
Codex. Passive snapshot reads do not request renewal. Native managed
credential-store/login restrictions override local settings. Keyring, auto and
ephemeral stores remain explicitly unsupported for bearer reuse: the current
app-server account protocol does not export an access token. Delegating all
inference to Codex is a different execution architecture and was not adopted.
Explicit reuse returns a redacted `auth_issue`; paths and bearer data never
reach the browser. Process/cache bounds and measurements live in
[RESOURCE_CONTROL.md](RESOURCE_CONTROL.md#native-codex-login-discovery).

`lib/oauth/codex_shared_login.py` binds one explicit owner, account and source.
Existing Tofu logins take precedence. Discovery runs in personal-mode provider
reconciliation; distributed deployments never borrow the server OS identity.
`TOFU_CODEX_SHARED_LOGIN=0` disables discovery/use. A remote personal server can
borrow from exactly one online owner-owned desktop advertising `codex_login=1`.
The agent must grant **Share the local Codex login with Tofu** in its permission
window/tray, or `--allow-codex-login`. Egress permission alone does not grant it.
The addressed `codex_login_read` command accepts only an expected account ID,
never a path. Zero/multiple devices do not trigger arbitrary selection.

Only the reference is persisted in Tofu's existing OAuth store. Desktop access
snapshots live in memory for 30 seconds, bounded by the launch-probed task budget
(up to 1,024 entries), with a 64 KiB token ceiling and no new worker. Agent
results use the existing bounded, acknowledged bridge outbox. Passive status
reads never enqueue a command; reconciliation/request recovery refresh the
snapshot with a five-second deadline. A 1,000-read synthetic local-file probe
on the development host measured median 38 µs / p95 51 µs, with a 301-byte
access-only snapshot; this measures adapter overhead, not network latency. Device loss/revocation invalidates cached
use. Logout serializes with discovery, clears snapshots, and records a durable
opt-out without modifying Codex. Explicit Login can reconnect without browser
authorization. Account changes fail closed rather than silently rebinding.
Source outages preserve ProviderAccess configuration. Tests use synthetic
credentials and isolate the real Codex home: `tests/oauth/test_codex_shared_login.py`.

## Tofu-DB pre-authority account layout

All seven `tenant.user.*` operations now compile through the experimental
Tofu-DB Transaction IR for differential certification. Account IDs, normalized
emails, and the owner-allocation sequence live in a tenant-global identity
scope; callers still enter through the existing authenticated admin boundary.
An exact-email witness protects the digest index from collision ambiguity.

Large profile metadata and the password envelope are stored in one bounded,
blob-capable profile. Role, status, and last-login state are separate compact
entities, so suspension and login bookkeeping do not rewrite large blobs.
Created-time and status/created-time indexes serve descending lists with a
10,000-row scan ceiling, a 1,000-row page ceiling, and an 8 MiB response cap.
The public projection never contains `password_hash`; only the dedicated
authentication operation returns it. Creation publishes the profile, state,
email claim, both list indexes, monotonic owner allocation, receipt, and
encrypted logical outbox record in one OCC transaction.

All eleven `credential.*` operations now use that pre-authority IR. A small
immutable core, compact mutable state, and bounded blob-capable settings record
share tenant-global exact-verified ID and SHA-256-hash indexes. The owner/tenant
boundary has an exact live count capped at 1,000 and a descending created-time
index; list responses fail closed above 8 MiB. Authentication and touch rewrite
only compact state, while account status is witnessed in the same OCC
transaction. Revocation atomically retires owner visibility and the live count
but retains the hash tombstone needed by recovery-only identify.

## Change map

| Change | First files | Required guard |
|---|---|---|
| Account fields or allocation | `lib/billing/users.py`, `operations_pkg/_tenant.py` | `tests/billing/test_identity_http_contract.py` |
| Request principal | `lib/api_keys/_context.py`, `lib/identity.py`, `routes/api_v1/auth.py` | `tests/identity/test_principal_context.py` |
| Credential lifecycle | `lib/api_keys/`, `operations_pkg/_credentials.py` | `tests/api/test_api_keys.py`, `tests/storage/test_storage_authority_sql_contract.py` |
| Device authentication | `lib/bridge_auth.py`, `routes/_bridge_caller.py` | `tests/browser/test_bridge_auth.py` |

Any new identity field starts in the machine contract, is carried explicitly
through `AuthContext` and `PrincipalContext`, and is tested for wrong-owner and
suspension behavior before a repository consumes it.

### Explicit subscription-page reuse

**Problem:** implicit reuse on Login was undiscoverable, and a user asking to
reuse credentials could unintentionally start another authorization.
**Decision:** the ChatGPT subscription card offers an explicit reuse action.
`POST /api/oauth/login` accepts `provider=codex, reuse_only=true`; the normal
owner boundary applies. It returns `status=success` and the redacted
`credential_source`, `status=unavailable` when no eligible reference exists,
or the existing error envelope when a bound source cannot be read. It never
falls through to PKCE/device authorization. Status advertises
`shared_login_reuse_supported`; the UI disables this action against older
servers that would ignore the new flag and start an authorization.
Existing independent Tofu login
is retained; the UI asks the user to disconnect before switching sources.
**Alternative rejected:** routing this button to ordinary Login would hide
the missing-source/desktop-permission condition behind another login prompt.

The typed `settings/codex-shared-login.ts` owner renders the entry, connection
source and inline retry guidance. It mounts inside the retained subscription
card through a narrow status projection bridge. A proxy rejecting POST with
404/405 uses the existing GET login route with the same `reuse_only=true`
restriction. Network timeouts, authorization denials and server failures are
not retried as GET. A bound-source HTTP 400 retains its redacted `auth_issue`
for actionable inline guidance instead of a generic transport message.
Alternative rejected: an ordinary login retry can initiate authorization;
discarding the HTTP 400 diagnosis hides the source recovery step.
Verification: shared-login
route tests and `tests/visual/test_visual_codex_shared_login.py`.

Subscription sign-in failures offer a shared-kit button that opens Settings →
Subscription sign-in through the existing `jumpToSetting('oauth')` action.
Typed missing-subscription causes survive local request preparation as
`oauth_reauth`; dispatch still treats preparation as an unsent neutral failure.
It routes the unchanged request to an independent eligible slot when one exists
and learns an owner-scoped cooldown (`docs/modules/llm_io.md`); when nothing else
qualifies, the original typed cause still surfaces as `oauth_reauth`.
Older stored Codex/Claude "subscription not logged in (no valid OAuth token)"
errors receive the same presentation. Other internal failures keep log guidance.
Navigation never starts OAuth, changes models or retries the failed turn.

### Shared Codex credential read recovery

Problem: a temporary native-command probe failure was presented as session
expiry even when the Codex login remained valid. Decision: before provider
ingress, a missing borrowed bearer gets one owner-checked, binding-checked
re-read under the existing refresh lock. Explicit recovery bypasses only cached
native timeout/command-unavailability, storage-full and explicit-home-unavailability
failures; healthy and policy-failure caches remain. Inside a startup-ceiling
refusal window that bypassing probe fails before spawn.
Codex still owns renewal. Logout or binding changes prevent recovery. Native metadata has
30-second freshness and a 120-second absolute verification age. Healthy reads
schedule one finite revalidation flight after freshness expires and return
without waiting for native startup. Explicit recovery and reads without a valid
verified source remain synchronous. Clear/exit drain the owned worker. Only command
timeout/unavailability or storage-full can reuse that successful snapshot while
the explicit home is verifiable and known shell, config, executable and
managed-policy stamps are unchanged; failures cannot extend its age. Policy
rejection and source changes revoke it. Arbitrary shell imports and external
managed-policy changes are bounded by the absolute age,
not claimed to be fully observable. Every bearer is still read and checked live.
Task streams retry the same request preparation at most three times within a
30-second admission window, only for a typed transient credential cause under
`LocalRequestPreparationError(provider_reached=false)`. Cancellation interrupts
backoff. A delivered tool callback forbids replay; provider-stream errors and
deterministic preparation failures never enter this recovery.
Missing/expired login retains `oauth_reauth`; source-read, policy and identity
failures use a warning envelope with explicit `authIssue`, `credentialSource`
and `providerReached=false`. Only transient read failures allow manual retry.
Alternative rejected: unconditional turn replay risks repeating tools; borrowing
another source or retaining bearer credentials would weaken authority.

### Native Codex request binding

The native application factory uses NativeCodexCredentialResolver with the supplied
principal/repository, never an inferred current user. Existing owner/tenant/account
metadata or the declared personal/shared-login boundary proves source ownership.
Grant semantics and source identity are checked around refresh; account changes
within or between rounds fail closed without moving existing history. The existing
outbound owner emits identity/affinity headers and refuses a changed expected
account. Missing source reads retain credential-unavailable attribution. No new
vault or refresh-token copy is created. Verify native-credentials ownership,
revocation, source changes, ordinary revision advancement and real native ingress
refusal tests; production default remains unpromoted.
