# Desktop clients and connection lifecycle

The [remote execution map](modules/remote_execution.md) owns code routing;
[installation](INSTALL.md) owns source/runtime environment provisioning.

## Client roles

| Client | Responsibility | UI/business authority |
| --- | --- | --- |
| Browser | Authenticated access to the selected service | Retained web application |
| Android | Native profiles, credentials/cookies, WebView and supervisor integration | Retained web application; `android/AGENTS.md` |
| Full desktop app | Local application lifecycle plus optional device control | Web application and shared runtime owners |
| Desktop Agent | Explicitly addressed local capabilities for a remote service | Device permissions and bridge protocol; no application server/database |

A client shell never creates a second conversation state machine or storage
authority. Conversation synchronization continues through the generated v3
schema served on HTTP v4. Device control is a separate authenticated capability
boundary, with owner/device addressing on enqueue, streams and settlement.

## Device connection and recovery

The [bridge v3 schema](../contracts/desktop_bridge_v3.schema.json) owns negotiation,
limits and replay lifetimes. An agent reports remaining execution slots and active
IDs. The server prioritizes cancellation, delivers only available new work, and
includes the addressed device plus the command's original pickup deadline.
Expired finite-deadline records reclaim only after execution and output ACK; a
monotonic server-time watermark refuses their stale replay. The clock conservatively
charges request elapsed time, so delayed first delivery cannot execute expired
work; very short pickup TTLs may be refused early. Legacy servers stay
compatible through pinned records and demand-driven local admission; incompatible explicit versions require an
upgrade, and an established v3 run never silently downgrades. Restart-durable
exactly-once device execution is not provided by this ephemeral bridge.

Each connection run owns one reusable HTTP Session and bounded wire prefixes.
Output-bearing polls ACK immediately; queued remainder drains before idle sleep.
Ordinary empty polls keep the async long-poll window. Waiters wake only for the
matching owner and explicit device. Proxy and CA configuration remain honored;
redirects and ambient netrc never change credential authority. Same-address
credential refresh can rebind a configured route in place.

Problem: unbounded lifetime ID retention and fixed worker limits eventually
refused useful work or underused larger devices. Decision: negotiated expiry
fences permit reclaim, while cached CPU/memory headroom determines resident
advisory delivery slots and resident memory budgets; overflow spools privately,
wire prefixes stay bounded, and shutdown joins execution before reclaiming spools. Alternatives rejected:
forgetting IDs without expiry replays side effects; growing an entire HTTP body
with resident capacity can produce permanent 413 failures. See the bounded
[resource measurements](RESOURCE_CONTROL.md#desktop-device-resources).

## Linux bundle integration and Agent login startup

For an extracted full-app Linux bundle, `bash install.sh --autostart` registers
the menu/icon and opts into desktop-login startup. `--no-autostart` disables it;
a plain rerun preserves the existing choice and refreshes an enabled entry when
the bundle moves. `--uninstall` removes owned integration entries only, retaining
the executable and all application data. This bundle script is distinct from the
source-checkout dependency installer described above.

Problem: copying an icon before validating/publishing the menu could corrupt a
previous installation on failure. Decision: preflight all inputs, acquire a
per-user writer lock, stage menu/icon/login candidates and backups, then publish;
errors or catchable termination roll back the entries. Symlink targets and an
uninstall from a superseded bundle fail without deleting the current entries.
A hard kill can leave a lock/backups for explicit recovery; rollback failure
reports and retains the backup path. Alternative rejected: independent writes
leave mixed versions and make an unsuccessful upgrade look installed.

The Agent's existing login-startup preference now uses HKCU Run on Windows, XDG
autostart on Linux and a per-user LaunchAgent on macOS. No administrator rights
or credentials in startup entries are needed. Publication is atomic on Unix;
a failed toggle keeps the prior entry and does not persist success. Unix entries
take effect at the next login. Native installer/build smoke gates remain the
platform release authority.
