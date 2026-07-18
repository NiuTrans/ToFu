# Prefix-Cache Deploy Acceptance — Deployer Checklist

> **Audience:** the person restarting `:15000` to deploy the cache-fix chain.
> **Scope:** this describes ONLY the mechanisms that already exist
> (`tests/cache_deploy_verdict.sh` + `tests/cache_acceptance_check.py`). It adds
> no code and asks nothing new of you beyond running one command.

The cache-fix chain (commits `ab161bf` str↔block · `1274cee` raw↔stripped ·
`0a9f6af` prefill-skip · `8ecbbcf` reasoning_content parity · `1920827`
single-source builder) is **committed in HEAD but only takes effect on the next
restart** — the running process compiled the old bytecode at its boot. "Client
can achieve near-100% prefix cache" is an *unverified claim* until post-restart
real traffic shows both already-cached-turn miss classes at zero.

---

## 0. How to swap the process

**Do not** hand-run `python server.py` while the old process is up: it loses the
`:15000` bind race, boots, and dies (observed as a storm of boot banners with no
`Ready`), while the OLD process keeps serving stale code. There are two paths;
the managed one is preferred because it self-heals.

### 0a. PREFERRED — supervisord-managed (install once, then one command forever)

The root cause of the repeated "restart didn't take" is that the server ran as a
**bare PPID=1 process nobody relaunches** — a crash/OOM left it dead and manual
launches raced the port. Hand its lifecycle to the host `supervisord` (autostart
+ autorestart) ONCE:

```bash
bash deploy/supervisor/install.sh
```

It validates `deploy/supervisor/tofu.conf`, installs it to
`/etc/supervisor/conf.d/`, does the one-time handoff (stops the hand-started
listener so supervisord owns the single instance), `reread`+`update`, and
verifies `RUNNING`. `/etc/supervisor/conf.d` is root-owned, so if the script
lacks root / passwordless-sudo it **prints the exact `sudo` commands and stops —
never a half-install**. After this, **every** restart is just:

```bash
sudo supervisorctl restart tofu     # graceful drain; crash/OOM self-heals
```

No more bare-process port races, and the server comes back on its own after any
death. `deploy/supervisor/tofu.conf` is guarded by `tests/test_supervisor_conf.py`.

### 0b. FALLBACK — restart_15000.sh (no supervisord)

If supervisord is not available/desired, use the idempotent kill-first script:

```bash
bash restart_15000.sh
```

It refuses if supervisord already owns tofu (use 0a then); takes a serialization
flock; kills the EXACT `:15000` PID (SIGTERM→SIGKILL, waits for the port to
free); relaunches detached (`setsid nohup`) from HEAD; then self-probes the
fixes are LIVE — including **(d) the served process self-reports
`CACHE_FIX_GEN >= 5`** — so a stale/wrong-tree boot fails loudly, not a false
"restarted".

> **Hard rule (both paths):** run from a shell that is **NOT a descendant of the
> `:15000` server** (a plain terminal — not a Tofu agent's `run_command` shell).
> Both scripts refuse a self-descendant to avoid killing the very shell running
> them; that guard is *why* an agent cannot restart the server for you — the
> operator must run it from an independent terminal.

After the server is up (managed or fallback), run the acceptance verdict below.

---

## 1. The one command

After you kill the old PID and restart from the current HEAD tree:

```bash
bash tests/cache_deploy_verdict.sh
```

It prints three self-explaining fact lines (a/b/c) + one machine-greppable
`ACCEPTANCE:` line, and exits **0 = READY**, **2 = WAIT**, **3 = FAIL**.

| Verdict | Meaning | Your next step |
|---|---|---|
| **READY** | Deployed (all 3 deploy proofs) **and** ≥150 post-boot `CacheStats` samples **and** both already-cached-turn miss classes at 0. | North star met on this run. Nothing more to do. |
| **WAIT** | Not deployed yet, or not enough traffic yet, or the loaded version can't be proven. | See §2 — the reason line names which sub-cause. Re-run after addressing it / after more traffic. |
| **FAIL** | Deployed + enough traffic, but a miss class **still fires** on already-cached turns. | A drift face remains. See §3 — hand the `field=[…]` line back for root-cause freeze. |

The verdict is **triple-gated** so `READY` can never false-green: it requires
`deployed=YES` = **boot_after_floor** (a new process, by the port-listener PID's
start time > the newest fix commit) **AND** **served_code=fresh** (the served
tree `/proc/<pid>/cwd` carries every fix source) **AND** **in_memory_gen ≥ 5**
(the process's own boot self-report of the bytecode it loaded). Disk-fresh alone
is not enough; the gen self-report is the proof of the *loaded* version.

**Restart tips that make the gates pass (learned the hard way):**
- Do a real **kill + restart**, not a hot-reload — a reload usually re-imports
  modules without changing the PID, and `cache.py`/`_run.py` bind their bytecode
  at import, so the loaded code stays old.
- After restart, confirm the swap yourself:
  `ss -ltnp | grep :15000` → the PID must be **new** (≠ the old one), and
  `ps -o lstart -p <newpid>` must be **> the newest fix commit time**.
- If a supervisor/systemd starts it, confirm it launches the **current HEAD
  working tree** (not an old deploy dir/image): `ls -l /proc/<newpid>/cwd`
  should point at this repo. The verdict checks this too, but confirming early
  saves a WAIT round.

---

## 2. What each `WAIT` sub-cause means (from the `-v`/fact lines)

Run with the wrapper (which passes `--verbose`) and read line **(a)**. Each
sub-cause is a distinct, honest "not proven deployed" — never a false green.

| Fact line shows | What it means | How to resolve |
|---|---|---|
| `boot_after_floor=NO` | The PID serving :15000 started **before** the newest fix commit — it's still the OLD process (reload didn't swap it, or supervisor re-spawned the old one). | Do a real kill + restart; verify a new PID with `ss`/`ps` as in §1. |
| `served_code=stale` | New PID, but its `/proc/<pid>/cwd` tree is a **partial/old copy** — the reason names the exact file + which fix is missing (e.g. `_toolcalls.py missing [single-source builder]`). | Restart from the current HEAD tree; ensure the deploy source is fully synced (no partial rsync / unmerged file). |
| `served_code=unknown` | The cwd (or a fix file under it) **couldn't be read** — non-Linux, permission, dead pid, or unexpected layout. | The tool refuses to guess. Verify manually: `ls -l /proc/<pid>/cwd` and grep the carve-out in its `lib/llm/cache.py`. |
| `in_memory_gen=(none)` | New PID, disk fresh, but the process printed **no** `CACHE_FIX_GEN` self-report in its boot window — an old build predating the self-report, or the boot line rotated out of `logs/app.log`. | Restart from a build that carries the gen self-report (current HEAD does); confirm the `[CacheFixGen] CACHE_FIX_GEN=…` boot line is in `logs/app.log`. |
| `in_memory_gen=<n>` with `>=base=NO` | The process **self-reports an OLD gen** (< 5) — it loaded stale bytecode at boot even though disk is fresh (source rsync'd after start / stale `.pyc`). | Real restart from the current tree so the loaded bytecode is the fixed version. |
| `(b) enough=NO` (`samples < 150`) | Deployed correctly, just **not enough post-restart traffic** yet to be statistically meaningful. | Let real traffic accrue, then re-run. Nothing is wrong. |

Line **(c)** shows the already-cached-turn miss counts
(`prefix_changed(inside=True)` and `bytes_diverged(inside=True)`); both must be 0
for READY. These count ONLY genuine already-cached-prefix breaks — benign
editable-tail flips (`inside_prior_cached_prefix=False`) and cold first rounds
are deliberately excluded, so the number is not inflated.

---

## 3. If it prints `FAIL` — the root-cause handoff

`FAIL` means the fix is genuinely loaded (all deploy gates passed) **and** there
was enough traffic, but a miss class still fires on an already-cached turn — a
residual drift face the five fixes didn't cover.

Hand the failing evidence back for a root-cause freeze:
- The field-level tracer names the exact diverging field. Grep the post-boot
  window for `WIRE BYTES DIVERGED … field=[…]` and
  `WIRE PREFIX CHANGED … inside_prior_cached_prefix=True` and share those lines.
- The new subclass will be root-frozen the same way the prior five were
  (tests-first + NEUTER + real-conversation proof), **not** waved off as a
  gateway/scheduling issue. The client-side wire path is where near-100% is won.

---

## 4. The north-star criterion (one line)

**Achieved only when `bash tests/cache_deploy_verdict.sh` prints `READY`** — i.e.
the fix is provably deployed (new PID + fresh served tree + self-reported
gen ≥ 5) **and** post-restart real traffic shows both already-cached-turn miss
classes at zero. Code correctness — however thoroughly proven offline — does not
substitute for that post-deploy real-traffic signal.
