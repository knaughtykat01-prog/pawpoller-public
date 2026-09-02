# Self-Hosting PawPoller — Step by Step

This guide takes you from **nothing** to a PawPoller instance running 24/7 on a
server you control, reachable over HTTPS from your phone.

It assumes no prior Docker or Linux experience. Every command is one you copy,
paste and run. Where you have to make a decision, the recommended option is
marked and the reason is given.

**Time:** about 30 minutes, most of it waiting.
**Cost:** free to about $6/month depending on which host you pick.

> **Do I even need this?** Only if you want PawPoller collecting stats while your
> computer is off. The desktop app does everything the server does — see
> [SETUP.md](SETUP.md) §1. Polling only happens while PawPoller is running, so if
> your machine sleeps, the numbers stop. That is the whole reason to self-host.

---

## Contents

1. [Pick where it runs](#1-pick-where-it-runs)
2. [Create the server](#2-create-the-server)
3. [Connect to it](#3-connect-to-it)
4. [Install Docker](#4-install-docker)
5. [Get PawPoller](#5-get-pawpoller)
6. [Configure it](#6-configure-it)
7. [Start it](#7-start-it)
8. [Reach it from anywhere, over HTTPS](#8-reach-it-from-anywhere-over-https)
9. [Finish setup in the browser](#9-finish-setup-in-the-browser)
10. [Keep it healthy](#10-keep-it-healthy)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Pick where it runs

PawPoller is small. It idles at roughly **250–400 MB of RAM** and does almost
nothing between polls, so the cheapest tier at any host is enough. What it needs
is to be **always on**.

| Option | Cost | Best for |
|---|---|---|
| **A spare computer at home** | Free (plus power) | You already have an old laptop or a Raspberry Pi 4/5 doing nothing |
| **Oracle Cloud Always Free** | Free, indefinitely | Free forever, but sign-up is fussy and capacity is often unavailable |
| **Hetzner Cloud** | ~€4/mo | Cheapest reliable VPS; EU and US regions |
| **DigitalOcean** | ~$6/mo | Friendliest console, best docs, one-click Docker image |
| **Google Cloud / AWS** | Varies | Only if you already use them — the consoles are far more complex |

**Recommended: DigitalOcean if you want it to just work, Hetzner if you want it
cheap, a spare machine at home if you have one.** The rest of this guide works
identically on all of them — only §2 differs.

**Minimum specs:** 1 vCPU, 1 GB RAM, 10 GB disk, any recent Linux. Ubuntu 24.04
LTS is assumed below because it is the most widely documented.

> **A note on home hosting:** it is genuinely the best option if you have the
> hardware, but do **not** forward a port from your router to it. Use the
> Cloudflare Tunnel method in §8 — it needs no open ports at all and is safer
> than anything you would configure by hand.

---

## 2. Create the server

Skip this section entirely if you are using a machine you already own — go to §3.

### DigitalOcean

1. Sign up at [digitalocean.com](https://www.digitalocean.com/) and add a payment
   method.
2. **Create → Droplets.**
3. **Region:** pick the one closest to you.
4. **Image:** Ubuntu 24.04 (LTS) x64.
5. **Size:** Basic → Regular → **$6/mo** (1 GB RAM / 1 vCPU / 25 GB). This is
   ample.
6. **Authentication:** choose **SSH Key** and click *New SSH Key*. If you do not
   have one, see [§3.1](#31-make-an-ssh-key) first, then come back and paste the
   public key. (Password auth works but is meaningfully less safe.)
7. **Hostname:** `pawpoller` — anything you like.
8. **Create Droplet.** After about 45 seconds you will see an **IP address** like
   `203.0.113.42`. Write it down; you need it in §3.

### Hetzner Cloud

1. Sign up at [hetzner.com/cloud](https://www.hetzner.com/cloud) — identity
   verification can take a few hours on a new account.
2. **New project → Add Server.**
3. **Location:** closest to you. **Image:** Ubuntu 24.04.
4. **Type:** Shared vCPU → **CX22** (or the cheapest available).
5. **SSH key:** add yours (see [§3.1](#31-make-an-ssh-key)).
6. **Create & Buy Now.** Note the **IP address** shown.

### Oracle Cloud Always Free

Genuinely free forever, but the sign-up is the hardest of the three and free
capacity is frequently exhausted in popular regions.

1. Sign up at [oracle.com/cloud/free](https://www.oracle.com/cloud/free/).
2. **Compute → Instances → Create Instance.**
3. **Image:** Canonical Ubuntu 24.04. **Shape:** `VM.Standard.A1.Flex` — set
   **1 OCPU / 6 GB RAM** (comfortably inside the free allowance).
4. Add your SSH public key. **Create.**
5. Note the **Public IP address**.
6. ⚠ **Oracle blocks all inbound ports by default, twice over.** You must open
   them in *both* the VCN security list *and* the instance firewall. If you use
   the Cloudflare Tunnel method in §8 you can skip that entirely — the tunnel
   makes only outbound connections. This is a strong reason to prefer it here.

---

## 3. Connect to it

### 3.1 Make an SSH key

Skip if you already have one (`~/.ssh/id_ed25519.pub` exists).

**Windows** (PowerShell), **macOS** or **Linux** (Terminal):

```bash
ssh-keygen -t ed25519 -C "pawpoller"
```

Press Enter three times to accept the defaults. Then print the **public** key to
paste into your host's console:

```bash
cat ~/.ssh/id_ed25519.pub
```

On Windows PowerShell use `type $env:USERPROFILE\.ssh\id_ed25519.pub`.

> Paste the `.pub` one. The file without `.pub` is your private key and must
> never leave your machine.

### 3.2 Log in

```bash
ssh root@YOUR-SERVER-IP
```

Replace `YOUR-SERVER-IP` with the address from §2. Type `yes` at the fingerprint
prompt. You are now on the server — everything from here runs there, not on your
own machine.

> Hetzner and Oracle may use `ubuntu@` rather than `root@`; the console tells you
> which. If you get `Permission denied (publickey)`, the key you pasted does not
> match the one on your machine.

### 3.3 Make a non-root user (recommended)

Running everything as `root` means one mistake can destroy the machine.

```bash
adduser pawpoller                  # asks for a password — pick a real one
usermod -aG sudo pawpoller
rsync --archive --chown=pawpoller:pawpoller ~/.ssh /home/pawpoller
```

Log out (`exit`) and back in as the new user:

```bash
ssh pawpoller@YOUR-SERVER-IP
```

---

## 4. Install Docker

One block, copy the whole thing:

```bash
sudo apt update && sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Let your user run Docker without `sudo`:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

Check it worked:

```bash
docker run --rm hello-world
```

You should see *"Hello from Docker!"*. If you get a permission error, log out and
back in — group changes only apply to new sessions.

---

## 5. Get PawPoller

```bash
git clone https://github.com/knaughtykat01-prog/pawpoller-public.git
cd pawpoller-public
```

You still clone the repository either way — it carries the compose file, the
`.env.example` you are about to copy, and the docs. What you choose next is
whether the server **builds** PawPoller or simply **downloads** it.

| | Build from source | Prebuilt image |
|---|---|---|
| Command | `docker compose up -d --build` | `docker compose -f docker-compose.image.yml up -d` |
| First start | 3–10 minutes | under a minute |
| Needs | ~2 GB RAM to build comfortably | any supported machine |
| Good for | forks, air-gapped installs, running a change before it is released | everyone else |

**If you are unsure, use the prebuilt image.** It is the same code, built by the
project's own release workflow, published for **linux/amd64 and linux/arm64** —
so it runs on a normal x86 VPS, on Oracle Cloud's free ARM tier, and on a
Raspberry Pi without any change.

> **Small servers should not build.** A 1 GB machine runs PawPoller perfectly
> well but is often killed part-way through compiling its dependencies, which
> looks like a hang or a `Killed` message with no explanation. The prebuilt
> image sidesteps that entirely.

---

## 6. Configure it

```bash
cp .env.example .env
nano .env
```

`nano` is a simple editor: arrow keys to move, type to edit, **Ctrl+O** then
Enter to save, **Ctrl+X** to quit.

You only need **two** things to start. Everything else can be done later in the
web interface, which is far easier than editing this file.

### 6.1 Set a dashboard password — not optional

Find these lines, remove the leading `#`, and set a long random password:

```
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=a-long-random-password-you-did-not-reuse
```

**Without this, anyone who reaches the port has full control — including reading
back every platform credential you store.** Generate one with:

```bash
openssl rand -base64 24
```

### 6.2 Set the vault key

Your platform credentials are always encrypted at rest. If you do not supply a
key, PawPoller generates one and stores it *next to* the encrypted file — which
means anyone who copies the data volume gets both halves. Supplying it yourself
keeps the key off the volume.

Generate one:

```bash
docker run --rm python:3.11-slim sh -c "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
```

Put it in `.env`:

```
PAWPOLLER_VAULT_KEY=the-key-you-just-generated
```

> ⚠ **Save this key somewhere safe, outside the server** — a password manager.
> Restoring a backup onto a new machine without it means re-entering every
> platform credential. Set it *before* first run; changing it later strands
> anything already encrypted.

### 6.3 Story archive (skip if you only post artwork)

If you write, point PawPoller at the folder holding your story folders:

```
PAWPOLLER_ARCHIVE_DIR=/home/pawpoller/story-archive
```

Then create it: `mkdir -p ~/story-archive`.

### 6.4 Platform credentials — leave them alone for now

The rest of `.env` is one block per platform. **Do not fill these in yet.** The
setup wizard in §9 walks you through them in the browser, explains where each
token comes from, and tests each one as you go. Editing them here is for
automation, not first-time setup.

---

## 7. Start it

**Prebuilt image** (recommended — see §5):

```bash
docker compose -f docker-compose.image.yml up -d
```

**Or build from source:**

```bash
docker compose up -d --build
```

The prebuilt image starts in well under a minute. Building takes **3–10
minutes** the first time. Later starts take seconds either way.

> Use the same command every time for a given server. The two files keep their
> data in the same place, but mixing them means Docker rebuilds or re-pulls
> unnecessarily.

Check it came up:

```bash
docker compose ps
curl -s http://localhost:8420/api/health
```

You want `{"status":"ok","version":"..."}`. If you get nothing, see §11.

> **It is not reachable from outside yet, on purpose.** PawPoller binds to
> `127.0.0.1` — the server itself — so it is not exposed while you are still
> setting up. §8 is how you reach it properly.

---

## 8. Reach it from anywhere, over HTTPS

**Do not simply open port 8420 to the internet.** It would be an unencrypted
login page, on a known port, guarding your accounts.

### Option A — Cloudflare Tunnel (recommended)

No open ports, no public IP, free TLS, and it works behind home routers and
Oracle's firewalls alike. You need a domain on Cloudflare (a cheap `.com` is
fine; Cloudflare's DNS is free).

```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
cloudflared tunnel login
```

That prints a URL — open it in your own browser and pick your domain. Then:

```bash
cloudflared tunnel create pawpoller
cloudflared tunnel route dns pawpoller pawpoller.yourdomain.com
sudo cloudflared service install
```

Create `/etc/cloudflared/config.yml`:

```yaml
tunnel: pawpoller
credentials-file: /root/.cloudflared/pawpoller.json
ingress:
  - hostname: pawpoller.yourdomain.com
    service: http://localhost:8420
  - service: http_status:404
```

```bash
sudo systemctl restart cloudflared
```

`https://pawpoller.yourdomain.com` now reaches your dashboard. Optionally add
**Cloudflare Access** in front of it for a second login layer.

### Option B — Caddy (if your server has a public IP and a domain)

Point an `A` record at your server's IP first, then:

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

Caddy fetches a Let's Encrypt certificate automatically within seconds.

> ⚠ Use `restart`, not `reload` — a reload does not always pick up a changed
> Caddyfile, and you will spend an hour debugging a config that is already
> correct.

Leave `PAWPOLLER_BIND` alone in both options. The proxy reaches PawPoller over
loopback; the port stays closed to the world.

---

### Install it on your phone or tablet

Once §8 is done and PawPoller answers on an `https://` address, it installs as an app on any
phone or tablet — no App Store, no separate build. It is a **PWA**, so the "app" is your own
instance running full-screen without browser chrome.

**iPhone / iPad:** open the address in **Safari** (this only works in Safari on iOS), press
**Share** → **Add to Home Screen**.

**Android:** open it in Chrome and take the **Install app** prompt, or **⋮ → Add to Home
screen**.

**Desktop:** Edge and Chrome show an install icon in the address bar.

This is why §8 is worth doing properly rather than leaving the instance on `localhost`: a
tunnelled or proxied address is reachable from the sofa, and the dashboard is designed to be
usable on a small screen.

> **One iOS quirk:** an installed home-screen app gets its own storage, separate from Safari's.
> A few purely local preferences will not carry across from the browser you set it up in. Nothing
> that matters — logins and settings live on the server — but it is why the app can feel very
> slightly "fresh" the first time you open it from the home screen.

---

## 9. Finish setup in the browser

Open your `https://` address. Log in with the `DASHBOARD_USER` and
`DASHBOARD_PASSWORD` from §6.1.

PawPoller detects a fresh install and sends you to a **setup wizard** instead of
the dashboard. Work through it: it asks which platforms you use, tells you
exactly where to get each token, and tests each connection before moving on.

Suggested order — start with one, confirm it polls, then add the rest:

1. **Inkbunny** or **FurAffinity** — most people's main gallery.
2. **Bluesky** — an app password takes ten seconds to create.
3. Everything else as you need it.

You do not have to connect all twenty. Connect what you use.

---

## 10. Keep it healthy

### Updating

**If you used the prebuilt image:**

```bash
cd ~/pawpoller-public
git pull
docker compose -f docker-compose.image.yml pull
docker compose -f docker-compose.image.yml up -d
```

**If you build from source:**

```bash
cd ~/pawpoller-public
git pull
docker compose up -d --build
```

The database migrates itself on startup and your data volumes are preserved.

> `git pull` matters in both cases — it refreshes the compose file and the docs.
> The `pull` step is what actually fetches the new version of the app when you
> are running the image; without it Docker keeps using the copy it already has.

### Backups

Everything lives in the `pawpoller-data` volume. Back it up on a schedule:

```bash
docker run --rm -v pawpoller-public_pawpoller-data:/data -v $(pwd):/backup \
  ubuntu tar czf /backup/pawpoller-backup-$(date +%F).tar.gz /data
```

PawPoller also has **Settings → Data → Backup & Restore**, which downloads a
single `.zip` of the database, settings, encrypted vault and media. That is the
easier option, and the one to use before an upgrade.

> Store backups **off the server**, and remember the vault key from §6.2 is not
> in them if you supplied it yourself. Keep both or the backup is undecryptable.

### Watching it

```bash
docker compose logs -f pawpoller      # live
docker compose logs --tail=200        # recent
docker compose restart pawpoller      # bounce it
```

### Automatic security updates

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

---

## 11. Troubleshooting

**`curl: (7) Failed to connect to localhost port 8420`**
The container is not running. `docker compose ps` shows its state and
`docker compose logs --tail=50` shows why it stopped.

**`permission denied while trying to connect to the Docker daemon`**
Your user is not in the `docker` group yet. Log out and back in.

**The build fails with a memory error**
1 GB of RAM is tight for a build. Add swap and rebuild:
```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**The site says "502 Bad Gateway"**
The proxy is up but PawPoller is not. Check `docker compose ps`.

**Cloudflare Tunnel connects but shows 404**
The `hostname` in `/etc/cloudflared/config.yml` does not match the DNS record.
They must be identical.

**I forgot the dashboard password**
Set a new `DASHBOARD_PASSWORD` in `.env` and run
`docker compose up -d --force-recreate`.

**Polling is not picking anything up**
Check **Settings → Platforms** for a red session indicator. The usual cause is
an expired cookie or token — cookie-based platforms need re-pasting every few
weeks, which the session-health panel warns about in advance.

**Some platform works on my desktop but not the server**
A few sites block datacenter IP ranges. PawPoller can route those through a
Cloudflare Worker — see the `CF_WORKER_URL` block in `.env.example`.

---

## Where things live

| What | Where |
|---|---|
| Database, settings, encrypted vault | Docker volume `pawpoller-data` → `/app/data` |
| Logs | Docker volume `pawpoller-logs` → `/app/logs` |
| Story archive | The host folder you set in `PAWPOLLER_ARCHIVE_DIR` |
| Configuration | `.env` next to `docker-compose.yml` |

Named volumes survive `docker compose down` and rebuilds. They are destroyed by
`docker compose down -v` — the `-v` is the dangerous part.
