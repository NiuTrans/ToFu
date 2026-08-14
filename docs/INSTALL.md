# Tofu — Install Guide

You only need this page if the one-command install in the README didn't
work, or if you want to pre-configure something (API key, port,
install directory, …).

For the happy path, just run the command from the README and skip this
file entirely.

---

## Pre-configuring the install (Linux / macOS)

Windows users: just run the `.exe` from the release page — no flags.
The sections below apply only to the Linux/macOS `install.sh`.

`install.sh` accepts the flags below. They write to `.env` and start
the server when finished.

| Flag | Purpose |
|---|---|
| `--api-key sk-xxx` | Pre-configure your LLM API key |
| `--port 8080` | Server port (default `15000`) |
| `--dir <path>` | Install directory (default `~/tofu`) |
| `--with-postgres` | Install + bootstrap PostgreSQL (opt-in). Default is SQLite — see below |
| `--use-conda` | Force the legacy conda path, skipping the default uv fast path (see below) |
| `--no-launch` | Install only; don't start the server |

Example:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --api-key sk-xxx --port 8080
```

---

## What the installer actually does

The installer is `install.sh`. It has two backends and picks the fast one
automatically:

### Fast path (default): uv

On a reasonably modern Linux host (glibc ≥ 2.28) or macOS, the installer
uses [uv](https://github.com/astral-sh/uv):

1. Ensures a `uv` binary (installs it from astral.sh if missing).
2. Creates a project-local virtualenv `.venv` from uv's own managed
   CPython (Python 3.12) — no system/conda interpreter needed.
3. `uv pip install -r requirements.txt` — installs the whole dependency
   stack from prebuilt manylinux wheels (typically ~1–2 min, **zero**
   from-source builds).
4. Runs an import smoke-test (`lxml`, `fitz`/PyMuPDF, `PIL`/Pillow,
   `cryptography`, …), then `scripts/verify_pdf_stack.py` checks the exact
   PyMuPDF trio and performs a real Markdown extraction. **If either check
   fails — e.g. an old glibc with no
   compatible wheel — it falls back cleanly to the conda path.**
5. Detects a system `ripgrep`/`fd` (optional speedups; the app degrades
   to a pure-Python search fallback if absent — never a source build).
6. Installs the Playwright Chromium binary (best-effort).
7. Writes `.tofu_env.json` (`backend: uv`) so `python server.py` re-execs
   into the venv even from an unactivated shell.

### Fallback path: conda

The installer switches to conda automatically when: `--use-conda` or
`--with-postgres` was passed, the host glibc is < 2.28 (PyMuPDF/Pillow
ship no manylinux2014 wheel for it), or the uv import smoke-test failed.
The conda path:

1. Locates conda, or installs a private Miniforge as a project sibling.
2. Creates a `tofu` conda env (Python 3.12).
3. Installs binary/system-sensitive dependencies from conda-forge. A small
   pip-only group is then installed into that exact env; the rich PDF trio is
   exact-pinned and installed together with dependency resolution. This avoids
   the `GLIBC_2.25 not found` trap for packages such as `lxml` while preventing
   a conda PyMuPDF + unrelated PyMuPDF4LLM split-brain.
4. Installs `ripgrep`, `fd-find`, and Chromium shared libs from
   conda-forge — no `sudo`, no system packages.
5. Installs the Playwright Chromium binary.
6. Writes `.tofu_env.json` so `python server.py` re-execs into the right
   interpreter even from an unactivated shell.

Both paths then configure `.env` and start the server.

To audit or repair PDF parsing after an environment change, run
`python scripts/verify_pdf_stack.py`. A healthy result reports all three pinned
versions and `Markdown smoke passed`; the command exits non-zero otherwise.

## Database: two equal backends, one sidecar

SQLite and PostgreSQL implement the same storage contract and capacity target.
SQLite is the zero-configuration default; PostgreSQL is enabled only by the
explicit `TOFU_DB_BACKEND=postgres` choice (the installer flag below provisions
its binaries). Neither backend is a fallback, archive tier, or reduced edition.
If the selected backend fails preflight or becomes unavailable, startup fails
closed and runtime never switches engines.

All application processes talk to the project-local Storage Sidecar. Only that
process loads a driver, opens `data/tofu.db` or `data/pgdata`, and owns
transactions. See [the authoritative storage contract](STORAGE_REDESIGN.md).

PostgreSQL provisioning remains opt-in because it adds its server binaries:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --with-postgres
```

**Selecting PostgreSQL or recovering a previous PG dataset:** re-run the
installer with `--with-postgres`, then explicitly set
`TOFU_DB_BACKEND=postgres`.
If you already have a `data/pgdata/` from an earlier install, that data
is reused (not lost) — the installer detects it and pins the matching PG
major. Until you pass `--with-postgres`, an existing `pgdata` is left in
place, unused, and the installer prints how to re-enable it.

---

## Troubleshooting

### "GLIBC_2.25 not found" or "GLIBC_2.28 not found" at startup

A pip-installed wheel of `lxml` (or similar) is shadowing the conda-forge
build. Fix it by re-running the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --reset-env
```

`--reset-env` deletes the existing conda env and rebuilds from scratch.
**Destructive** — only use it on the Tofu-owned env.

### Conda solver hangs forever

Almost always an outdated `conda` you installed yourself. Easiest fix:
let the installer drop a private Miniforge next to the project, and use
that instead of touching yours:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --force-sibling-conda
```

### PostgreSQL "data directory was created by major version X" error

Your `data/pgdata/` was initialized by a different PG major than the one
the installer just placed in your env. Two options:

1. **Install the matching PG major** and retry the selected backend. The app
   must not fall back to SQLite.
2. **Re-initialize PG** — only after a verified backup, pass `--reinit-pgdata`. The installer renames
   the old `pgdata` to `pgdata.bak.<timestamp>` and creates a fresh one.

To deliberately select SQLite instead, change the explicit selector before
startup:

```bash
curl -fsSL …/install.sh | bash -s -- --force-sqlite
```

### Behind a corporate proxy that blocks GitHub releases

The Miniforge download will fail. Workarounds:

- Pre-download `Miniforge3-<platform>.sh` manually and set
  `TOFU_MINIFORGE_LOCAL=/path/to/Miniforge3-...sh` before running the
  installer.
- Or set `TOFU_MINIFORGE_MIRRORS="https://your-mirror/..."` (one URL per
  line) — the installer tries each in order before giving up.

### Install log

Every `install.sh` run writes a full transcript (ANSI colours stripped)
to `<install-dir>/logs/install-YYYYMMDD_HHMMSS.log`. Attach it when
filing an issue.

---

## Database overrides

| Env var | Effect |
|---|---|
| `TOFU_DB_BACKEND=sqlite|postgres` | The sole backend selector; default `sqlite`, invalid values fail closed |
| `TOFU_STORAGE_SQLITE_READ_POOL` | SQLite query-only pool size (default 16) |
| `TOFU_STORAGE_PG_READ_POOL` / `TOFU_STORAGE_PG_WRITE_POOL` | PostgreSQL pool requests (32/16, automatically capped to the 80% server budget) |
| `TOFU_STORAGE_MIN_FREE_BYTES` | Project-filesystem startup floor |
| `TOFU_STORAGE_PREFLIGHT_MAX_MS` | Maximum accepted filesystem preflight latency |

---

## Docker

```bash
git clone https://github.com/rangehow/ToFu.git && cd ToFu
docker compose up -d
```

All data persists in named Docker volumes. No flags needed.
