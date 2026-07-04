# Tofu Desktop Builds

Standalone installers for Windows, macOS, and Linux — no Python, no conda, no terminal needed.

## Quick Build (local)

```bash
# 1. Install build dependencies
pip install -r requirements.txt
pip install -r desktop/requirements-desktop.txt

# 2. Generate platform icons
python scripts/gen_desktop_icons.py

# 3. Build
pyinstaller tofu.spec

# Output: dist/Tofu/
# Run:    dist/Tofu/Tofu        (Linux/macOS)
#         dist/Tofu/Tofu.exe    (Windows)
```

## What's included

The installer bundles:
- Python 3.12 runtime
- Flask + all backend dependencies
- PostgreSQL support (psycopg2 driver — PG server bootstraps on first launch)
- SQLite as automatic fallback when PG is unavailable
- Playwright Python package (browser binary downloaded separately)
- Frontend (HTML/CSS/JS — no build step)
- System tray icon with Open/Quit/Install Components controls
- Auto-opens browser on startup

## First-Launch Experience

The **installer only ships files and launches the app** — it never shells out to
download components (a PyInstaller `--onedir` bundle has no standalone
`python.exe`, so the old installer `[Run]` step that called
`_internal\python.exe -m playwright install` was dead code that also silently
downloaded nothing). Instead, on first launch the **app itself** shows a dialog
asking which optional components to download:

| Component | Size | Default | Purpose |
|---|---|---|---|
| **PostgreSQL Database** | ~50 MB | ✅ Recommended | Auto-bootstraps a local PG instance for full concurrency + JSONB + FTS |
| **Browser Engine (Chromium)** | ~150 MB | ✅ Recommended | Enables JS-rendered page fetching and browser automation |

When frozen, the Chromium download relaunches `Tofu.exe` with
`TOFU_PLAYWRIGHT_INSTALL=1` (handled in `desktop/launcher.py`), which drives the
bundled `playwright` package in-process — the correct way to reach the
interpreter inside a onedir bundle.

Users can skip and install later via the tray menu: **Right-click → Install Components...**

If both are skipped, the app still works — it uses SQLite for storage and skips browser-dependent features.

## Install location & writable data

The Windows installer installs **per-user** to
`%LOCALAPPDATA%\Programs\Tofu` (`PrivilegesRequired=lowest`, no UAC/admin
prompt). This matters: the app keeps its `data/` (config, SQLite DB, PostgreSQL
data dir) and `logs/` **next to the executable**, so the install dir must be
user-writable. A `Program Files` install under `lowest` privileges is NOT
writable and used to crash the app on first launch. If the exe dir ever ends up
read-only anyway, the backend falls back to a per-user data dir
(`%LOCALAPPDATA%\Tofu`) — see `lib/runtime_paths.py`.

## Architecture

```
dist/Tofu/
├── Tofu(.exe)        ← Main executable (system tray launcher)
├── _internal/        ← Frozen Python + all packages + app code
│   ├── static/       ← Frontend assets
│   ├── index.html
│   ├── lib/          ← Backend logic (incl. database dual-backend)
│   ├── routes/       ← Flask blueprints
│   └── ...
└── data/             ← User data (created on first run, portable)
    ├── config/       ← Server config, API keys
    ├── pgdata/       ← PostgreSQL data directory (auto-created)
    └── tofu.db       ← SQLite fallback (only if PG unavailable)
```

## Database Strategy

The desktop build uses the same dual-backend architecture as the server deployment:

1. **Primary: PostgreSQL** — auto-bootstrapped as a local userspace process (no admin/sudo needed). The PG server binary is either downloaded on first launch or discovered from the system PATH.
2. **Fallback: SQLite** — if PG bootstrap fails (no binary, no network), the app seamlessly falls back to SQLite. Fully functional for single-user use.

Users never need to think about databases — it Just Works.

## CI / Automated Builds

Push a version tag to trigger builds on all platforms:

```bash
git tag v0.10.0
git push origin v0.10.0
```

This runs `.github/workflows/build-desktop.yml` which produces:
- `Tofu-Setup-0.10.0-win64.exe` — Windows installer (Inno Setup)
- `Tofu-0.10.0-macos.dmg` — macOS disk image
- `Tofu-0.10.0-linux-x86_64.tar.gz` — Linux portable archive

Artifacts are attached to a draft GitHub Release.

## Manual workflow dispatch

Go to Actions → "Build Desktop" → "Run workflow" to build without tagging.

## Code Signing (optional)

For distribution without OS warnings:
- **Windows**: Get an EV code signing certificate, add to GitHub Secrets as `WIN_CERT_PFX` + `WIN_CERT_PASSWORD`
- **macOS**: Enroll in Apple Developer Program ($99/yr), add signing identity to the workflow

Without signing, users will see SmartScreen (Windows) or Gatekeeper (macOS) warnings on first launch.

## Adding New Optional Components

1. Create a new `Component` subclass in `desktop/post_install.py`
2. Implement `is_installed()` and `install()` methods
3. Add to `OPTIONAL_COMPONENTS` list
4. Set `recommended = True` if it should be pre-checked in the dialog

## Updating

The desktop build currently has no auto-update mechanism. Users download a new installer from the Releases page.

Future: integrate [Sparkle](https://sparkle-project.org/) (macOS) or [winsparkle](https://winsparkle.org/) (Windows) for delta updates.
