# PawPoller Setup Guide

This is a new-user install guide. Pick the mode that fits you:

| Mode | Who it's for | Data stays on | Dashboard reachable from |
|------|--------------|---------------|--------------------------|
| **Desktop (Windows)** | One user, one machine, Windows | Your PC | `127.0.0.1:8420` only |
| **Desktop (Linux)** | One user, one machine, Linux | Your PC | `127.0.0.1:8420` only |
| **Docker / headless** | Self-hosted, always-on polling, access from phone/laptop | Your server | LAN or the public internet (you choose) |
| **From source** | Devs, macOS users, anyone wanting to hack on it | Wherever you clone | Same as whichever entry point you run |

All four share the same code, database, and UI — the only difference is how it's packaged and where the process runs.

**macOS**: native desktop build is on the roadmap. For now run via Docker or from source.

---

## 1. Desktop (Windows)

The simplest path on Windows. Single-file installer or portable zip, your choice.

### 1.1 Requirements

- Windows 10 or 11 (x64)
- Microsoft Edge installed (used for PDF generation — it's preinstalled on modern Windows)
- ~200 MB disk space for the app, more for stories

### 1.2 Install

Two formats — pick whichever:

**A) Installer (recommended)**

1. Open the [Releases page](https://github.com/knaughtykat01-prog/PawPoller/releases/latest).
2. Download `PawPoller-Setup-{version}.exe`.
3. Run it. Windows SmartScreen will warn ("Windows protected your PC") because the installer is unsigned — click **More info → Run anyway**.
4. The installer defaults to a per-user install (no UAC prompt). Tick the boxes for **Desktop shortcut** and **Run on Windows startup** if you want them.
5. **Launch PawPoller now** is ticked on the final page; clear it if you want to start manually.

To uninstall, use **Add or Remove Programs**. The uninstaller will offer to keep your `%APPDATA%\PawPoller\` data folder (default Yes — your SQLite DB / settings / vault survive a reinstall).

**B) Portable zip**

1. Download `PawPoller-windows-x64.zip` from the same Releases page.
2. Right-click → Properties → **Unblock** (Windows sometimes flags unsigned zips). Then extract anywhere — e.g. `C:\PawPoller`.
3. Double-click `PawPoller.exe`.

On first launch a pywebview window opens with the setup wizard. The app also installs a tray icon — right-click it for **Show / Hide / Quit**.

### 1.3 First-run wizard

The wizard walks you through four steps:

1. **Welcome** — overview.
2. **Story archive path** — point at a folder where your stories live (or will live). See §5 for the expected layout.
3. **Platform connections** — cards for all 17 platforms. You can skip this and fill it in later from Settings.
4. **Done**. Dashboard opens.

### 1.4 Where your data lives

```
%APPDATA%\PawPoller\           (equivalent to C:\Users\<you>\AppData\Roaming\PawPoller)
├── data\
│   ├── pawpoller.db            SQLite database (polling stats, publications, queue)
│   ├── settings.json           Non-secret settings
│   └── settings.vault.json     Encrypted credentials (vault is always on — see §5.1)
└── logs\                       Rotated log files
```

Back these two folders up and you've got everything.

### 1.5 Updating

In-app: the auto-updater checks GitHub for new releases and surfaces a notification with a one-click update flow.

Manual: download the new release zip / installer, install over the old version (the installer upgrades in place; for the zip, extract over the old folder while keeping your `data\` and `logs\` folders untouched), re-launch. The database auto-migrates.

### 1.6 Uninstalling

Three ways, all use the same uninstaller under the hood:

1. **Windows Search** — type "pawpoller", right-click → **Uninstall**.
2. **Settings → Apps & features** — find PawPoller in the list, click → **Uninstall**.
3. **Control Panel → Programs and Features** — same.

The Inno Setup uninstaller asks once for confirmation, then offers to also delete `%APPDATA%\PawPoller\` (default **No** — keeps your data so a reinstall picks back up).

If you installed via the **portable zip**, there's no Add/Remove Programs entry. Use the in-app **Settings → General → Danger zone → Uninstall PawPoller** button instead — it builds a `.bat` that waits for the process to exit, then removes the install folder + optionally your data folder + autostart entry. Type `UNINSTALL` in the confirm box.

Manual fallback for the portable zip:

```powershell
# Remove the folder you extracted PawPoller into (no installer = no auto-cleanup)
Remove-Item -Recurse -Force "C:\Path\To\PawPoller"
# Optional: remove user data + autostart + keyring entry
Remove-Item -Recurse -Force "$env:APPDATA\PawPoller"
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "PawPoller" -ErrorAction SilentlyContinue
cmdkey /delete:PawPoller   # if vault was enabled
```

### 1.7 Build from source (advanced)

If you'd rather build the exe yourself:

```powershell
git clone https://github.com/knaughtykat01-prog/PawPoller.git
cd PawPoller
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pyinstaller
python -m PyInstaller pawpoller.spec --noconfirm
# Output: dist\PawPoller\PawPoller.exe
```

To also build the Inno Setup installer, install [Inno Setup 6](https://jrsoftware.org/isinfo.php) and run `iscc /DMyAppVersion="<version>" installer\PawPoller.iss`. Output lands in `installer\Output\`.

---

## 1B. Desktop (Linux)

Single-file AppImage. No install, no root, no package manager — `chmod +x` and run.

### 1B.1 Requirements

- x86_64 Linux with glibc 2.35 or newer
  - **Tested on**: Ubuntu 22.04+, Fedora 37+, Debian 12+, Arch
  - Older distros (Ubuntu 20.04, Debian 11) won't run the AppImage — use the Docker mode or build from source on a newer base
- ~200 MB disk space
- Optional: `libnotify-bin` (`sudo apt install libnotify-bin` on Debian/Ubuntu, equivalents on other distros) for desktop toast notifications. The AppImage works without it; you just won't see toast pop-ups.

### 1B.2 Install

```bash
# Replace {version} with the actual release tag, e.g. 2.25.0
wget https://github.com/knaughtykat01-prog/PawPoller/releases/latest/download/PawPoller-{version}-x86_64.AppImage
chmod +x PawPoller-*.AppImage
./PawPoller-*.AppImage
```

Or grab it from the [Releases page](https://github.com/knaughtykat01-prog/PawPoller/releases/latest) in a browser, then `chmod +x` and double-click in your file manager.

The first launch opens the setup wizard. PawPoller installs a system-tray icon via libappindicator on KDE / GNOME (with the AppIndicator extension installed) / XFCE / MATE / Cinnamon.

### 1B.3 First-run wizard

Same four steps as Windows — see §1.3 above.

### 1B.4 Where your data lives

```
~/.local/share/PawPoller/      (XDG_DATA_HOME)
├── data/
│   ├── pawpoller.db
│   ├── settings.json
│   └── settings.vault.json
└── logs/
```

Some Linux distros put `%APPDATA%`-equivalent data under `~/.config/PawPoller/` depending on Python's `appdirs` resolution. Check the app's startup log if you're not sure — `[INFO] Config dir: …` is logged at boot.

### 1B.5 Run on login

Settings → General → **Run on Windows startup** (the label is generic) toggle writes a `.desktop` entry to `~/.config/autostart/PawPoller.desktop`. Standard XDG autostart spec — honoured by GNOME, KDE, XFCE, Cinnamon, MATE, LXQt automatically.

### 1B.6 Updating

In-app: the auto-updater downloads the new AppImage and replaces the file at `$APPIMAGE` in place, then re-execs. The path of the currently-running AppImage is preserved.

Manual: download the new AppImage, replace the old file, `chmod +x`, re-launch.

### 1B.7 Uninstalling

The cleanest path is **Settings → General → Danger zone → Uninstall PawPoller** in the dashboard. Tick which of these to remove (all three are checked by default):

- **Application files** — the `.AppImage` itself, found via the `$APPIMAGE` env var
- **User data** — your SQLite DB, settings, vault, logs (`~/.local/share/PawPoller/`)
- **Autostart entry** — the `.desktop` file under `~/.config/autostart/`

Type `UNINSTALL` in the confirm input, click Uninstall. The dashboard shows a goodbye screen; the process spawns a detached shell script that waits 3s, removes whatever you chose, and exits. Close the browser tab when done.

Manual fallback — if you'd rather just `rm` it yourself:

```bash
rm -f /path/to/PawPoller-*.AppImage          # the AppImage
rm -rf ~/.local/share/PawPoller              # data
rm -f ~/.config/autostart/PawPoller.desktop  # autostart
# Optional: secret-tool clear service PawPoller user vault_key   # vault key
```

### 1B.7 Build from source (advanced)

```bash
git clone https://github.com/knaughtykat01-prog/PawPoller.git
cd PawPoller
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # Pulls PyQt6 + PyQt6-WebEngine for Linux
pip install pyinstaller

# System runtime deps (Ubuntu / Debian — adjust for your distro)
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 \
                 libgdk-pixbuf-2.0-0 libffi8 fonts-dejavu-core libnotify-bin \
                 libgl1 libegl1 libxkbcommon-x11-0 libdbus-1-3 \
                 libnss3 libxcomposite1 libxdamage1 libxrandr2 libasound2 \
                 libfuse2          # only needed for the AppImage step

python -m PyInstaller pawpoller.spec --noconfirm
# Output: dist/PawPoller/PawPoller

# Optional: bundle as an AppImage
./installer/build-appimage.sh <version>
# Output: installer/Output/PawPoller-<version>-x86_64.AppImage
```

---

## 2. Docker / headless (self-hosted, web-accessible)

Best for leaving PawPoller polling 24/7 and reaching the dashboard from any device on your LAN — or, behind a reverse proxy, from the public internet.

### 2.1 Requirements

- Linux host (any distro with Docker — tested on Debian 12 and Ubuntu 22/24). Windows/WSL and macOS Docker Desktop also work.
- Docker Engine ≥ 24 and Docker Compose v2 (ships with modern Docker).
- A directory on the host for your story archive (shared via bind mount).

### 2.2 Clone and configure

```bash
git clone https://github.com/knaughtykat01-prog/PawPoller.git
cd PawPoller
cp .env.example .env
```

Edit `.env`. Uncomment only the platforms you use — fill in credentials or leave blank and configure via the UI later. **Always set `DASHBOARD_PASSWORD`** if the container will be reachable by anything other than `localhost`:

```
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=pick-something-long-and-random
```

Without a password, anyone who can hit port 8420 gets full control — including your credentials.

### 2.3 Point PawPoller at your story archive

The container bind-mounts a host directory as the story archive (where your `MASTER.md` files and generated formats live). Set it in `.env`:

```
PAWPOLLER_ARCHIVE_DIR=/home/YOU/story-archive
```

If unset it defaults to `./story-archive` next to `docker-compose.yml`. The two named volumes (`pawpoller-data`, `pawpoller-logs`) are managed by Docker and survive rebuilds; the archive bind-mount is yours to back up. (Skip this if you don't use the story-publishing side.)

### 2.4 Start it

```bash
docker compose up -d --build
```

Check it's up:

```bash
docker compose ps
docker compose logs --tail=50
curl -s http://localhost:8420/api/health
```

Visit `http://localhost:8420` on the host and log in with the credentials you set. (By default the dashboard is loopback-only — to reach it from another device, see §2.5.)

### 2.5 Exposing it to the web

**Don't just open port 8420 to the internet directly.** The dashboard is hardened (bcrypt, optional TOTP, optional Turnstile, rate limiting) but sits behind a single password — you want TLS in front of it. Pick one:

**Option A — Cloudflare Tunnel (easiest, no port-forwarding, no public IP needed):**

```bash
# On the host:
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
cloudflared tunnel login                       # opens browser, pick your domain
cloudflared tunnel create pawpoller
cloudflared tunnel route dns pawpoller pawpoller.yourdomain.com
cloudflared tunnel --url http://localhost:8420 run pawpoller
# Run as a service:  sudo cloudflared service install
```

Now `https://pawpoller.yourdomain.com` reaches the dashboard over TLS, no inbound ports open. You can also enable Cloudflare Access in front of it for an extra auth layer.

**Option B — Caddy reverse proxy (if you already have a domain pointed at your server):**

```caddy
pawpoller.yourdomain.com {
    reverse_proxy localhost:8420
}
```

Caddy provisions a Let's Encrypt cert automatically.

**Option C — nginx reverse proxy:**

```nginx
server {
    listen 443 ssl http2;
    server_name pawpoller.yourdomain.com;
    ssl_certificate     /etc/letsencrypt/live/pawpoller.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pawpoller.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8420;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Whichever proxy you use, keep port 8420 itself off the public internet — your reverse proxy should reach it over loopback. **This is the default:** PawPoller binds `127.0.0.1:8420` unless you set `PAWPOLLER_BIND`. Only expose it directly (`PAWPOLLER_BIND=0.0.0.0` in `.env`) if you know what you're doing and have `DASHBOARD_PASSWORD` set.

### 2.6 Updating

```bash
cd PawPoller
git pull
docker compose up -d --build
```

The SQLite schema auto-migrates on startup. Your `data` and `logs` volumes are preserved; the `story-archive` bind mount is never touched by the container except on explicit saves from the editor.

### 2.7 Watching what it's doing

```bash
docker compose logs -f pawpoller       # stream live logs
docker compose logs --tail=200         # last 200 lines
docker compose restart pawpoller       # bounce without rebuild
```

### 2.8 Backup

Three things to snapshot:

1. `.env` (your credentials, if you used it — otherwise settings.vault.json has them).
2. The `pawpoller-data` named volume — or just the DB inside it:
   ```bash
   docker compose exec pawpoller sqlite3 /app/data/pawpoller.db ".backup /app/data/backup.db"
   docker cp $(docker compose ps -q pawpoller):/app/data/backup.db ./pawpoller-backup.db
   ```
3. Your story archive directory (the bind mount).

### 2.9 Throwaway test instance (QA)

For testing the first-run wizard and empty-state UI without touching your real instance, there's a parallel compose file that brings up a separate `pawpoller-test` container on port **8421** with its own throwaway volumes.

```bash
cp .env.test.example .env.test                                # one-time setup
docker compose -f docker-compose.test.yml up -d --build       # launch on :8421
```

Now hit `http://localhost:8421` — fresh database, no settings, you'll go through the setup wizard like a brand-new install.

To wipe and start over:

```bash
docker compose -f docker-compose.test.yml down -v             # -v drops the volumes too
docker compose -f docker-compose.test.yml up -d --build       # back to zero
```

Notes:
- The test instance has no `story-archive` bind mount, so testing publishing flows uses files inside the container only.
- Your prod instance on `:8420` is completely unaffected — separate container name, separate volumes.
- `.env.test` is gitignored. Never paste real platform credentials in it; the whole point is exercising the empty-state setup.

---

## 3. From source (Linux / macOS / Windows dev)

If you want to run it outside Docker — for development, custom packaging, or a Linux desktop install.

### 3.1 Requirements

- Python 3.11 or 3.12 (3.13+ untested)
- pip + venv
- For PDF rendering on Linux: `weasyprint` needs `libpango`, `libcairo`, `libgdk-pixbuf`. Ubuntu: `sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0`.
- For desktop mode: a display server (won't launch pywebview headlessly).

### 3.2 Clone and install

```bash
git clone https://github.com/knaughtykat01-prog/PawPoller.git
cd PawPoller
python -m venv .venv
source .venv/bin/activate               # Windows: .venv\Scripts\Activate.ps1

# Pick the right requirements file:
pip install -r requirements.txt         # desktop (includes pywebview + pystray)
# or
pip install -r requirements-server.txt  # headless (no GUI deps)
```

### 3.3 Run

```bash
python main.py      # desktop mode — opens a pywebview window + tray icon
# or
python server.py    # headless — serves the dashboard on 0.0.0.0:8420
```

Visit `http://localhost:8420` in a browser.

### 3.4 Run as a systemd service (Linux headless)

Create `/etc/systemd/system/pawpoller.service`:

```ini
[Unit]
Description=PawPoller
After=network.target

[Service]
Type=simple
User=pawpoller
WorkingDirectory=/opt/PawPoller
Environment="PATH=/opt/PawPoller/.venv/bin"
EnvironmentFile=/opt/PawPoller/.env
ExecStart=/opt/PawPoller/.venv/bin/python server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pawpoller
sudo systemctl status pawpoller
journalctl -u pawpoller -f
```

Same reverse-proxy advice as §2.5 applies.

---

## 4. Story archive layout

PawPoller reads and writes stories from a single parent directory. Each story is one subfolder:

```
story-archive/
├── Late_Shift/
│   ├── story.json                  metadata (title, author, chapters, tags, platform IDs, …)
│   ├── Markdown/
│   │   └── MASTER.md               canonical source — you edit this
│   ├── BBCode/                     auto-generated from MASTER.md (Inkbunny)
│   ├── SoFurry_HTML/               auto-generated (SoFurry)
│   ├── Styled_HTML/                auto-generated (AO3 work skin / preview)
│   ├── SquidgeWorld_HTML/          auto-generated (SqW)
│   ├── PDF/                        auto-generated (WeasyPrint / Edge)
│   └── cover.png                   optional cover image
└── My_Second_Story/
    └── Nice_Version/               stories can be nested one level deep
        └── Markdown/MASTER.md
```

You can either:

- **Create new stories from the UI** — the "Create New Story" wizard scaffolds everything with a template MASTER.md.
- **Import from a platform** — Settings → Import → paste an Inkbunny/SoFurry/FurAffinity URL. Content is downloaded, converted to Markdown, and saved as a new story folder.
- **Drop in existing files manually** — put a `MASTER.md` under `<StoryName>/Markdown/`, regenerate formats from the editor, and fill out the metadata drawer.

MASTER.md uses HTML-comment anchors (`<!-- @title -->`, `<!-- @body -->`, etc.) to tell the converter how to render each section. The editor's toolbar inserts them for you — hover any button for a tooltip with an example.

---

## 5. Platform credentials

Different platforms need different auth mechanisms. A couple of the awkward ones (FA, X) need session cookies — the desktop app's **"Login via Browser"** button pops up a real browser window, you log in, and it captures the cookies for you. In headless/Docker mode you paste cookies manually (instructions are on each platform's settings card). DeviantArt uses the official API instead: register a DA app and paste its client_id/client_secret (no cookie).

Short version per platform:

| Platform | Needs | Where to get it |
|----------|-------|-----------------|
| Inkbunny | Username + password | Your IB account — and tick "Enable API access" in IB's account settings |
| FurAffinity | `a` and `b` cookies | Log in to FA, DevTools → Application → Cookies → copy `a` and `b` |
| SoFurry | Personal Access Token | sofurry.com → Settings → Developer → New token ([direct link](https://sofurry.com/settings/pat-create)). Paste the token; your profile handle is read from it automatically. **No password needed** — and 2FA accounts work fine, because PawPoller never logs in |
| Weasyl | API key | weasyl.com → Settings → API keys → generate |
| AO3 | Username + password | Your AO3 account |
| SquidgeWorld | Username + password | Your SqW account |
| DeviantArt | App client_id + client_secret | deviantart.com/developers/apps → Register Application (Client type: Confidential) → copy client_id + client_secret; also enter the DA username to track |
| Mastodon | Instance URL + access token | Your instance → Preferences → Development → New application (scope `read`) |
| Tumblr | OAuth consumer key + blog | tumblr.com/oauth/apps → register an app → copy the "OAuth Consumer Key" |
| Pixiv | Refresh token | One-time browser login (e.g. the `gppt` helper) |
| Threads | Meta access token | A Meta app with `threads_basic` + `threads_manage_insights` scopes |
| e621 | Username + API key | Account → Manage API Access on e621 (the API key, not your password) |
| Wattpad / Itaku / Bluesky / X | Public or app-password — see the in-app settings cards | |

### 5.1 Credential vault (always on)

Credentials are **always** stored in an encrypted vault (`settings.vault.json`) — plaintext
credential storage does not exist. There is nothing to enable; anything that lands in
`settings.json` in plaintext (an old backup, a hand edit) is swept into the vault at the next
startup. What varies is **where the encryption key lives**:

- On **Windows desktop**, the key goes in Windows Credential Manager — separate from the
  ciphertext, so this is genuine at-rest protection out of the box.
- On **Linux/Docker with no keyring**, the key falls back to `data/.vault_key` (chmod 600)
  **on the same volume as** `settings.vault.json`. Anyone who can read the volume reads both,
  so this protects against casual/off-host snooping (stray backups, image layers) but is
  **not** real at-rest encryption against someone with host/volume access.
- **For real at-rest protection on a server**, supply the key yourself so it lives off the
  data volume: set `PAWPOLLER_VAULT_KEY` (or point `PAWPOLLER_VAULT_KEY_FILE` at a mounted
  Docker/K8s secret) — ideally before first run, but you can also move an existing
  `data/.vault_key`'s contents into the env var and delete the dotfile. Generate a fresh key
  with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
  and keep it in your secrets manager. Back the key up separately from `settings.vault.json` —
  losing one makes the other useless (credentials are re-enterable, but it's sixteen platforms
  of re-entering).

Settings → Credential Security shows which key store is in use.

### 5.2 Two-factor auth

Settings → Security → Enable 2FA. Scan the QR into any TOTP app (Aegis, 1Password, etc.). Recovery codes are shown once — write them down.

### 5.3 API keys

For scripting (pause/resume polling, trigger regens), Settings → Security → API Keys → generate. Use as `Authorization: Bearer pp_xxx`.

### 5.4 Cloudflare Turnstile (optional)

Adds a Turnstile challenge to the login form. Useful if you've exposed the dashboard publicly and want bot-protection in front of password auth. Set the site key + secret in Settings → Security → Turnstile.

---

## 6. Common gotchas

- **"Port 8420 already in use"** — another PawPoller or unrelated process. `lsof -i :8420` (Linux) or `Get-NetTCPConnection -LocalPort 8420` (PowerShell) to find it.
- **PDFs blank on Linux/Docker** — missing WeasyPrint system libs. `apt-get install libpango-1.0-0 libpangoft2-1.0-0` and rebuild.
- **"AO3 login keeps failing from my server"** — AO3 sometimes puts Cloudflare Shields-up on datacenter IPs. Route through the Cloudflare Worker proxy (CF_WORKER_URL in `.env`) or run AO3 posting from the desktop.
- **"FurAffinity only works on desktop"** — correct, by design. FA blocks most datacenter IPs; the server mode auto-queues FA posts for the desktop app to process when it's next online.
- **"My stories disappeared after `docker compose up`"** — you probably didn't set `PAWPOLLER_ARCHIVE_DIR` (§2.3), so the container mounted the default empty `./story-archive`. Check `docker compose config` to see the resolved path.
- **"I forgot the dashboard password"** — with Docker running, `docker compose exec pawpoller python -c "from auth import reset_admin_password; reset_admin_password('newpassword')"`.

---

## 7. Where to go next

- The heavily-commented source — start with `dashboard.py`, then a platform under `clients/{xx}/` and its `polling/{xx}_poller.py`
- [Releases](https://github.com/knaughtykat01-prog/PawPoller/releases) — what shipped in each version
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — dev setup, adding a new platform, code style
- [`ROADMAP_PUBLIC.md`](ROADMAP_PUBLIC.md) — what's planned

If something's missing or wrong, open an issue — this guide gets updated as the app evolves.
