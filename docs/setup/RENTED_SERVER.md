# Path C: a rented server

You rent a small computer in a data centre (a "cloud server") and PawPoller runs there around
the clock, in **Docker** (a tool that runs PawPoller in its own sealed box). This guide assumes
you're comfortable in a terminal: for the generic parts (making a server, installing Docker or
Tailscale) it points you at the official guides, which stay current. If
[Path B](HOME_SERVER.md) would work for you, it is simpler.

Not sure this is the right guide? [Start here](../SETUP.md#start-here-which-of-these-are-you).

**Cost:** about US$6 a month. **Time:** about an hour, much of it waiting.

The steps use **DigitalOcean**, which has the friendliest setup. Other hosts work too: see
[Other cloud hosts](#other-cloud-hosts).

**Step C1. Make a key to log in with.**
Cloud servers are safest when you log in with a *key* (a pair of files on your computer) instead
of a password. Make one with DigitalOcean's guide,
[Add SSH keys](https://docs.digitalocean.com/products/droplets/how-to/add-ssh-keys/).

- **Only ever share the file ending in `.pub`.** The other one is your private key and never
  leaves your computer.

**Step C2. Create the server.**
Follow DigitalOcean's [Create a Droplet](https://docs.digitalocean.com/products/droplets/how-to/create/).
What PawPoller needs:

- **Image:** Ubuntu 24.04 (LTS) x64.
- **Size:** Basic → Regular → the **$6/mo** option (1 GB RAM). That is plenty.
- **Authentication:** the SSH key from Step C1, not a password.
- **Hostname:** `pawpoller`.

- **You'll see:** after about a minute, an **IP address** such as `203.0.113.42`. Write it down.

**Step C3. Log in to the server.**
Connect as `root` with DigitalOcean's
[Connect with SSH](https://docs.digitalocean.com/products/droplets/how-to/connect-with-ssh/),
for example `ssh root@203.0.113.42`.

- **You'll see:** the prompt changes to something like `root@pawpoller:~#`. You are now typing
  on the server. Every command from here runs there.

**Step C4. Install Docker (the tool that runs PawPoller).**
Follow Docker's [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
(the `apt` repository method). It includes the `docker compose` plugin PawPoller uses.
Check it worked:

```bash
docker run --rm hello-world
```

- **You'll see:** **Hello from Docker!**

**Step C5. Download PawPoller.**

```bash
git clone https://github.com/knaughtykat01-prog/pawpoller-public.git
cd pawpoller-public
cp .env.example .env
```

**Step C6. Set your password.**
First, have the server make up a strong password for you:

```bash
openssl rand -base64 24
```

Copy what it prints. Then open the settings file:

```bash
nano .env
```

`nano` is a simple editor: use the arrow keys to move and type to edit. Find these two lines,
delete the `#` at the start of each, and put your password after the `=`:

```
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=paste-the-password-here
```

Save with **Ctrl+O** then Enter, and leave with **Ctrl+X**.

- **Keep this password somewhere safe**, such as a password manager. It's how you'll log in.

**Step C7. Set your vault key.**
Your site logins are stored encrypted. This makes the key that unlocks them:

```bash
docker run --rm python:3.11-slim sh -c "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
```

Copy what it prints, open `.env` again with `nano .env`, and add this line with your key after
the `=`:

```
PAWPOLLER_VAULT_KEY=paste-the-key-here
```

Save and leave as before.

- **Keep this key safe too, away from the server.** Without it, a backup can't unlock your saved
  logins, and you'd have to enter them all again.

**Step C8. Start PawPoller.**

```bash
docker compose -f docker-compose.image.yml up -d
```

Wait a minute, then check it's running:

```bash
curl -s http://localhost:8420/api/health
```

- **You'll see:** `{"status":"ok","version":"…"}`
- **If it goes wrong:** no reply means it hasn't started. `docker compose -f docker-compose.image.yml logs --tail=50`
  shows why.

**Step C9. Give it a private address with Tailscale.**
Still on the server, install Tailscale with Tailscale's [Linux install guide](https://tailscale.com/kb/1017/install)
and run `tailscale up`. Sign in with the account you'll use on your phone. Then, back on the server:

```bash
tailscale serve --bg 8420
```

- **You'll see:** your address, like `https://pawpoller.tail1234.ts.net`. Write it down.
- **If it goes wrong:** if it shows a link and says the feature isn't enabled, open the link,
  enable it, and run the command again.

**Step C10. Install Tailscale on your phone and your computer.**
Install Tailscale on each device you'll use PawPoller from, signed in with the **same account**
([Tailscale's install guide](https://tailscale.com/kb/1017/install), or Steps
[B6](HOME_SERVER.md) and B8 of Path B).

**Next:** open PawPoller and finish setup. That part is the same for every path:
[After you install](AFTER_INSTALL.md#open-pawpoller-and-finish-setup).

---

## Updates

Log in to the server (Step C3), then run:

```bash
cd pawpoller-public
./update.sh
```

Prefer a button? [Update from the dashboard](#update-from-the-dashboard-docker) sets up an
**Update now** button, or daily automatic updates.

**If an update goes wrong.** Every update first saves a copy of your database, then checks
the new version started properly. If it didn't, the update says so and prints one line to
go back, like this:

```bash
./update.sh --to 1a2b3c4
```

**Step 1.** Copy that line exactly as the update printed it and run it. You are back on the
version you had, with your data as it is now.

**Step 2.** The next plain `./update.sh` goes forward to the newest version again. Wait for
the next release if the problem was in the update itself.

The database copies are in the `backups` folder inside PawPoller's data (the newest five are
kept). You only need one of them if your data itself went wrong. Ask for help before
restoring one, because it replaces everything since that update.

---

## Other cloud hosts

PawPoller is small (it idles at 250–400 MB of memory), so the cheapest tier anywhere is enough.
Any host that gives you an **Ubuntu** server works with Path C from Step C3 onwards.

| Host | Cost | Notes |
|---|---|---|
| **DigitalOcean** | ~$6/mo | Used in Path C. Friendliest console. |
| **Hetzner Cloud** | ~€4/mo | Cheapest reliable option. New accounts can wait a few hours for identity checks. Choose **Shared vCPU → CX22**, Ubuntu 24.04, and add your SSH key. |
| **Oracle Cloud Always Free** | Free | Free forever, but sign-up is fussy and free capacity is often unavailable. Choose Ubuntu 24.04, shape `VM.Standard.A1.Flex` with 1 OCPU / 6 GB. Oracle blocks all incoming connections by default; Tailscale (Step C9) doesn't need any, which makes it the easy option here. |
| **Google Cloud e2-micro** | Free tier, with catches | One `e2-micro` in `us-west1`, `us-central1` or `us-east1`, a 30 GB **standard** disk and 1 GB of traffic a month are free. A second VM or a *balanced* disk costs money, every public IPv4 address is billed (about US$3.65/month), and traffic to Australia and China isn't in the free allowance. |

Some hosts log you in as `ubuntu@` instead of `root@`. Their console tells you which. If you log in
as a normal user, put `sudo` in front of the commands in Steps C4, C8 and C9.

---

## Update from the dashboard (Docker)

A container can't rebuild itself, so the **Update now** button (**Settings → About & updates →
Server updates**) is carried out by a small helper on the server. Install it once:

```bash
sudo server-update/install.sh
```

To have the server also update itself daily:

```bash
sudo server-update/install.sh --auto
# or later:  sudo systemctl enable --now pawpoller-auto-update.timer
```

Automatic updates are off unless you turn them on. Turn them off with
`sudo systemctl disable --now pawpoller-auto-update.timer`.

`./update.sh` itself: `./update.sh --check` only says whether a newer version exists; `--quiet`
prints only on a change or an error (for cron). It stops rather than overwrite local edits to a
tracked file. Under the hood it backs up the database (SQLite's backup API, inside the
container, to `data/backups/pre-update-*.db`), runs `git pull --ff-only`, then either
`docker compose up -d --build` (built from source, with the commit baked in) or
`docker compose -f docker-compose.image.yml pull` and `up -d` (prebuilt image). It then waits
for `/api/health` to answer on the new commit, runs `PRAGMA quick_check`, and reads the startup
log. A crash or a damaged database exits non-zero with the rollback command. `--to <commit>`
goes to one exact commit, and a plain run afterwards returns to the branch.

---

## Docker in more detail

### Prebuilt image or build it yourself

| | Build from source | Prebuilt image |
|---|---|---|
| Command | `docker compose up -d --build` | `docker compose -f docker-compose.image.yml up -d` |
| First start | 3–10 minutes | under a minute |
| Needs | ~2 GB RAM to build comfortably | any supported machine |
| Good for | forks, air-gapped installs, running a change before it is released | everyone else |

The prebuilt image is the same code, built by the project's release workflow for linux/amd64 and
linux/arm64, so it runs on a normal server, Oracle's free ARM tier and a Raspberry Pi. **Small
servers shouldn't build:** a 1 GB machine is often killed part-way through, which looks like a hang
or a `Killed` message. If you must build on one, add swap first:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Use the same command every time on a given server. Mixing the two makes Docker rebuild or re-pull
for no reason.

### Story folder on a server

If you write, point PawPoller at the folder holding your story folders. In `.env`:

```
PAWPOLLER_ARCHIVE_DIR=/root/story-archive
```

and create it (`mkdir -p ~/story-archive`). If it isn't set, PawPoller uses an empty
`./story-archive` next to `docker-compose.yml`. `docker compose config` shows the path in use.

### Watching it

```bash
docker compose logs -f pawpoller      # live
docker compose logs --tail=200        # recent
docker compose restart pawpoller      # restart it
```

(Using the prebuilt image, add `-f docker-compose.image.yml` after `docker compose`.)

### Backups from the command line

```bash
docker run --rm -v pawpoller-public_pawpoller-data:/data -v $(pwd):/backup \
  ubuntu tar czf /backup/pawpoller-backup-$(date +%F).tar.gz /data
```

Named volumes survive `docker compose down` and rebuilds. `docker compose down -v` deletes them:
the `-v` is the dangerous part.

| What | Where |
|---|---|
| Database, settings, encrypted vault | Docker volume `pawpoller-data` → `/app/data` |
| Logs | Docker volume `pawpoller-logs` → `/app/logs` |
| Story archive | The host folder you set in `PAWPOLLER_ARCHIVE_DIR` |
| Configuration | `.env` next to `docker-compose.yml` |

### A non-root user

Path C runs as `root` to keep the steps short. To use a normal account instead:

```bash
adduser pawpoller                  # asks for a password
usermod -aG sudo pawpoller
rsync --archive --chown=pawpoller:pawpoller ~/.ssh /home/pawpoller
usermod -aG docker pawpoller
```

Log out and back in as `ssh pawpoller@YOUR-SERVER-IP`, and put `sudo` in front of system commands.

### Automatic security updates

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

### A throwaway test instance

`docker-compose.test.yml` brings up a separate `pawpoller-test` container on port **8421** with its
own throwaway volumes, for trying the first-run wizard without touching your real instance:

```bash
cp .env.test.example .env.test
docker compose -f docker-compose.test.yml up -d --build       # http://localhost:8421
docker compose -f docker-compose.test.yml down -v             # wipe it
```

Never put real platform credentials in `.env.test`.
