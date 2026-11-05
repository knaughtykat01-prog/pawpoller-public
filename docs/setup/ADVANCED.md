# Advanced

Everything below is optional. You won't need it for Paths A, B or C.

The steps here mention steps from the install guides: B-steps are in
[Path B](HOME_SERVER.md), C-steps in [Path C](RENTED_SERVER.md). Docker-only extras (other
cloud hosts, the **Update now** button, logs, backups from the command line) are at the end of
[Path C](RENTED_SERVER.md#other-cloud-hosts).

---

## A public web address (instead of Tailscale)

Tailscale keeps PawPoller private to your own devices. If you'd rather open it at your own web
address, such as `https://pawpoller.yourdomain.com`, from any browser on any device, follow the steps
below. They work for Path B and Path C, and you can keep Tailscale as well.

**Never open port 8420 straight to the internet.** It would be a login page with no encryption,
at a number everyone knows. These steps don't open anything.

### Use a web address you already own

You need:

- **a domain name** you own, bought from any seller (Cloudflare, Namecheap, GoDaddy and so on);
- **a free Cloudflare account.** Cloudflare looks after the address and carries visitors to
  PawPoller through a private link called a **tunnel**. Nothing on your computer or server is
  opened to the internet, and Cloudflare provides the padlock (encryption) for free.

**Step 1. Put a password and two-factor login on PawPoller first.**
Anyone on the internet will be able to reach your login page, so both are required.

- **Password:** Step B4 (Path B) or Step C6 (Path C).
- **Two-factor login:** in PawPoller, go to **Settings → Security → Two-Factor Authentication**.
  Scan the code with an authenticator app on your phone (Google Authenticator, Microsoft
  Authenticator and Aegis all work). Keep the **recovery codes** it shows somewhere safe. They
  are shown once.

**Step 2. Add your domain to Cloudflare.**
Skip this step if your domain is already on Cloudflare (for example, you bought it there).

1. Sign up at [dash.cloudflare.com](https://dash.cloudflare.com/).
2. Go to **Domains → Onboard a domain**. Type your domain (like `yourdomain.com`) and click
   **Continue**.
3. Choose the **Free** plan. On the list of records that follows, click **Continue**.
4. Cloudflare shows you **two nameservers**, addresses ending in `ns.cloudflare.com`. Sign in
   where you bought the domain, find its **nameservers** setting, and replace what's there with
   Cloudflare's two.

- **You'll see:** an email from Cloudflare saying your domain is active. It is often under an
  hour and can take up to a day. The domain stays with the seller you bought it from. Only its
  nameservers change.
- **If it goes wrong:** if your seller has **DNSSEC** switched on for the domain, switch it off
  there before changing the nameservers, or the domain can stop working while it moves.

**Step 3. Create the tunnel.**

1. In Cloudflare, go to **Networking → Tunnels** and click **Create a tunnel**.
2. Name it `pawpoller` and click **Create Tunnel**.
3. Pick the system PawPoller runs on: **Windows** or **Mac** for Path B, **Debian** for Path C
   (it covers Ubuntu) or for Path B on Linux. Cloudflare shows an install command.
4. Copy the command and run it where PawPoller runs:
   - **Path B on Windows:** in an administrator PowerShell window (see Step B1). If Cloudflare
     shows a download to install first, install it, then run the command.
   - **Path C:** on the server. Log in as in Step C3.
5. Wait until Cloudflare shows the tunnel as connected, then click **Continue**.

- **You'll see:** the tunnel listed as **Healthy** under **Networking → Tunnels**.
- **Keep the command private.** The long code in it lets whoever has it run your tunnel.

**Step 4. Point your address at PawPoller.**
Cloudflare now asks where the tunnel should lead. If it doesn't, open the tunnel from
**Networking → Tunnels**, go to **Routes**, and click **Add route → Published application**.

1. **Subdomain:** `pawpoller`
2. **Domain:** pick yours from the list.
3. **Service URL:** `http://127.0.0.1:8420` (use `8421` if you changed it in
   [Using the desktop app as well](HOME_SERVER.md#extra-you-already-use-the-desktop-app)).
4. Click **Add route**.

**Step 5 (Path C only). Let PawPoller see who's visiting.**
On a rented server, PawPoller runs inside Docker, which hides your visitors' addresses unless you
add one line. Without it, every visitor looks like the same person, so a stranger's wrong password
guesses would lock you out too. On the server:

```bash
cd pawpoller-public
nano .env
```

Add this line at the end:

```
PAWPOLLER_FORWARDED_IPS=127.0.0.1,172.16.0.0/12
```

Save with **Ctrl+O** then Enter, leave with **Ctrl+X**, and restart PawPoller:

```bash
docker compose -f docker-compose.image.yml up -d
```

- **Never put `*` on that line**, even if another guide suggests it. With `*`, a visitor can
  pretend to be the server itself.

Path B doesn't need this step. There, PawPoller runs directly on your computer, and the tunnel
reaches it from the same machine.

**Step 6. Send everyone to the padlocked address.**
In Cloudflare, open your domain, go to **SSL/TLS → Edge Certificates**, and switch on
**Always Use HTTPS**. Anyone who types `http://` is then moved to the encrypted `https://` address.

**Step 7. Open it.**
On any device, go to `https://pawpoller.yourdomain.com` and log in.

- **You'll see:** the PawPoller login page, with a padlock in the address bar.
- **If it goes wrong:**
  - **Error 1033:** the tunnel isn't running on your computer or server. Run the install command
    from Step 3 again.
  - **502 Bad Gateway:** the tunnel is running but can't reach PawPoller. Check PawPoller is
    running (Step B3 or C8) and that the port in Step 4 is right.

**To take the address down,** delete the route (or the whole tunnel) under **Networking →
Tunnels**. PawPoller itself is untouched.

### Caddy or nginx (for people who already run a web server)

These suit a server with a public IP address and your domain pointed straight at it (an `A`
record). They need ports 80 and 443 open, which the tunnel above avoids.

#### Caddy

```bash
sudo apt install -y caddy
sudo nano /etc/caddy/Caddyfile
```

Replace the contents with:

```caddy
pawpoller.yourdomain.com {
    reverse_proxy localhost:8420
}
```

```bash
sudo systemctl restart caddy
sudo ufw allow 80,443/tcp && sudo ufw allow OpenSSH && sudo ufw --force enable
```

Caddy gets a certificate by itself within seconds. Use `restart`, not `reload`: a reload doesn't
always pick up a changed Caddyfile.

#### nginx

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

With either, leave `PAWPOLLER_BIND` alone. The proxy reaches PawPoller over the server's own
loopback connection, and port 8420 stays closed to the world. Only set `PAWPOLLER_BIND=0.0.0.0` if
you know why you need it and have a password set. On Docker, add the `PAWPOLLER_FORWARDED_IPS` line
from [Step 5](#use-a-web-address-you-already-own) as well. Without it, every visitor looks like the
proxy to PawPoller, so one person's wrong sign-in codes count against everyone. Put the proxy's
own address there, never `*`.

### Sign-in codes over plain http (home network only)

The desktop app's **Sign in for a server** sends your site sign-in to the server. Over https or
Tailscale that trip is encrypted. Over a plain address like `http://192.168.1.20:8420` it is not:
anyone else on the same Wi-Fi could copy it and post as you. So PawPoller refuses plain http by
default.

If you can't use https or Tailscale, you can allow it for a server in your own home:

1. On the server, open **Settings → Security → Sign-ins over plain http**.
2. Type your PawPoller password and tick the box saying you understand the risk.
3. Press **Allow plain http**.
4. On your computer, when you first sign in for that server, PawPoller asks once more. Press
   **Yes, allow plain http for this server**.

It only works for home-network addresses (like `192.168.…`, `10.…` or a `.local` name), never for
an address on the internet, and never on a hosted seat. **Better:** Tailscale (two devices: the
server and your computer) or a web address with https. Turn it off again on the same screen.

If a web server on the same machine passes plain-http visitors on to PawPoller, PawPoller sees them
all as the machine itself and can't tell they arrived unencrypted. Your computer's own check still
refuses anything that isn't a home address, but serve the proxy over https.

## The credential vault

Credentials are **always** stored encrypted (`settings.vault.json`). There is nothing to switch on.
What varies is **where the key lives**:

- **Windows desktop:** in Windows Credential Manager, separate from the encrypted file. Real
  protection with nothing to set up.
- **Linux or Docker with no keyring:** in `data/.vault_key`, **next to** the encrypted file. Anyone
  who can read the data folder can read both, so this guards against stray copies and backups, not
  against someone with access to the server.
- **Real protection on a server:** supply the key yourself so it lives outside the data folder, as
  in Step C7 (`PAWPOLLER_VAULT_KEY`, or `PAWPOLLER_VAULT_KEY_FILE` pointing at a mounted secret).
  Ideally do it before first run; you can also move an existing `data/.vault_key` into the
  variable and delete the file. Make a new key with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

Back the key up separately from `settings.vault.json`. Losing either makes the other useless.
**Settings → Security** shows which key store is in use.

## Security settings

- **Two-factor login:** Settings → Security → Two-Factor Authentication. Scan the code into any
  authenticator app. Recovery codes are shown once: write them down.
- **API keys** (for scripts, and for connecting the desktop app to a server): Settings → Security →
  API Keys. Use as `Authorization: Bearer pp_xxx`.
- **Cloudflare Turnstile** (optional): adds a bot check to the login page when the dashboard is
  public. Set the site key and secret under Settings → Security.
- **The first password can only be set from the computer PawPoller runs on.** Someone who reaches
  a brand-new install over the network can't claim it first. To allow it on a network you trust,
  set `PAWPOLLER_ALLOW_OPEN_SETUP=1`.

More detail: [security/SELF_HOST_SECURITY.md](../security/SELF_HOST_SECURITY.md).

## Running from source

For development, or on systems without a build.

- Python 3.11 or 3.12, pip and venv.
- For PDFs on Linux: `sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0`.
- The desktop mode needs a display.

```bash
git clone https://github.com/knaughtykat01-prog/pawpoller-public.git
cd pawpoller-public
python -m venv .venv
source .venv/bin/activate               # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt         # desktop (window + tray)
# or
pip install -r requirements-server.txt  # server only
python main.py                          # desktop
# or
python server.py                        # server, dashboard on port 8420
```

To run a source checkout as a Linux service, create `/etc/systemd/system/pawpoller.service`:

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

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pawpoller
journalctl -u pawpoller -f
```

(The Path B installer does all of this for you, from a prebuilt release.)

### Building the desktop app

Windows:

```powershell
git clone https://github.com/knaughtykat01-prog/pawpoller-public.git
cd pawpoller-public
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pyinstaller
python -m PyInstaller pawpoller.spec --noconfirm
# Output: dist\PawPoller\PawPoller.exe
```

For the installer too, install [Inno Setup 6](https://jrsoftware.org/isinfo.php) and run
`iscc /DMyAppVersion="<version>" installer\PawPoller.iss` (output in `installer\Output\`).

Linux (Ubuntu or Debian; adjust package names for your distro):

```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 \
                 libgdk-pixbuf-2.0-0 libffi8 fonts-dejavu-core libnotify-bin \
                 libgl1 libegl1 libxkbcommon-x11-0 libdbus-1-3 \
                 libnss3 libxcomposite1 libxdamage1 libxrandr2 libasound2 \
                 libfuse2          # only needed for the AppImage step
python -m PyInstaller pawpoller.spec --noconfirm        # dist/PawPoller/PawPoller
./installer/build-appimage.sh <version>                 # installer/Output/…AppImage
```

## Story archive layout

PawPoller reads and writes stories from one parent folder. Each story is a subfolder:

```
story-archive/
├── Late_Shift/
│   ├── story.json                  metadata (title, author, chapters, tags, platform IDs, …)
│   ├── Markdown/
│   │   └── MASTER.md               the file you edit
│   ├── BBCode/                     made from MASTER.md (Inkbunny)
│   ├── SoFurry_HTML/               made from MASTER.md (SoFurry)
│   ├── Styled_HTML/                made from MASTER.md (AO3 work skin / preview)
│   ├── SquidgeWorld_HTML/          made from MASTER.md (SqW)
│   ├── PDF/                        made from MASTER.md
│   └── cover.png                   optional cover image
└── My_Second_Story/
    └── Nice_Version/               stories can be nested one level deep
        └── Markdown/MASTER.md
```

You can create stories from the app (**Create New Story** sets everything up), import one from a
site (paste an Inkbunny, SoFurry or FurAffinity link), or drop in a `MASTER.md` under
`<StoryName>/Markdown/` and regenerate the formats from the editor.

`MASTER.md` uses comment markers (`<!-- @title -->`, `<!-- @body -->` and so on) to tell the
converter how to render each section. The editor's toolbar inserts them for you; hover any button
for an example.

## Where to go next

- [Releases](https://github.com/knaughtykat01-prog/pawpoller-public/releases): what changed in each version.
- [`CONTRIBUTING.md`](../../CONTRIBUTING.md): working on PawPoller's code.
- [`ROADMAP_PUBLIC.md`](../ROADMAP_PUBLIC.md): what's planned.

If something here is missing or wrong, open an issue on the project page.
