# Getting PawPoller Running

This guide gets PawPoller working for you, one step at a time. You do not need to know anything
about servers or code. Every command you need is printed in full: you copy it, paste it, and
press Enter.

Each step has one job. Under most steps you'll find:

- **You'll see:** what should happen, so you know it worked.
- **If it goes wrong:** the most common problem at that step, and the fix.

---

## Start here: which of these are you?

Pick the one that sounds most like you, then follow only that path.

| If this sounds like you… | Follow | Time |
|---|---|---|
| "I want to try PawPoller on my own computer." | [Path A: the desktop app](#path-a-the-desktop-app) | 10 minutes |
| "I want it collecting my stats all the time, and I have a computer that stays on. It can be the one I use every day. I'd like to check it from my phone too." | [Path B: always on, on a computer you own](#path-b-always-on-on-a-computer-you-own) | 30 minutes |
| "I don't have a computer that stays on, and I'm happy to rent a small server for about US$6 a month." | [Path C: a rented server](#path-c-a-rented-server) | About an hour |

**Not sure?** Start with Path A. It is the quickest way to see whether PawPoller is for you.

> **Why "always on" matters.** PawPoller collects your stats by checking your sites every so often.
> It can only do that while it is running. If the computer it runs on is asleep or switched off,
> the checking pauses, and so do any posts you've scheduled. Paths B and C exist to keep it running.

### Words you'll meet

- **Dashboard**: PawPoller's screens, where you see your stats and post your work.
- **Server**: a copy of PawPoller that runs quietly in the background with no window, so it keeps
  working while you get on with other things. You open its dashboard in a web browser.
- **PowerShell** (Windows) or **Terminal** (Mac and Linux): a window where you paste a command and
  press Enter. You'll only need it for Paths B and C.
- **Tailscale**: a free app that links your own devices (your computer, your phone) into a
  private network. Your phone can then reach PawPoller from anywhere, and nobody else can.

---

## Path A: the desktop app

PawPoller runs as a normal app on your computer. It collects stats and posts while it's open.

**What you need:** a Windows 10 or 11 computer, or Linux. (There is no Mac app yet. On a Mac,
follow [Path B](#path-b-always-on-on-a-computer-you-own) instead. It works on a Mac.)

### A1. Windows

**Step 1. Download the installer.**
Open the [PawPoller downloads page](https://github.com/knaughtykat01-prog/pawpoller-public/releases/latest).
Under **Assets**, click the file named `PawPoller-Setup-` followed by a version number, for
example `PawPoller-Setup-4.32.2.exe`.

- **You'll see:** the file appear in your Downloads folder.

**Step 2. Run it.**
Double-click the downloaded file.

- **You'll see:** a blue box saying **"Windows protected your PC"**. This is expected. PawPoller
  is not signed with a paid certificate, so Windows doesn't recognise the publisher. Click
  **More info**, then **Run anyway**.
- **If it goes wrong:** if there is no **Run anyway** button, your computer's security settings
  block unrecognised apps. Ask whoever manages the computer, or use the portable version in
  [Part 2](#installing-without-the-installer-portable-zip).

**Step 3. Work through the installer.**
Click **Next** through the pages. Two boxes are worth a look:

- **Create a desktop shortcut**: tick it if you'd like an icon on your desktop.
- **Launch PawPoller when Windows starts**: tick it if you want PawPoller to start by itself
  every time you log in. This is how you keep your stats coming in without thinking about it.

On the last page, leave **Launch PawPoller now** ticked and click **Finish**.

- **You'll see:** a small window checking for updates for a few seconds, then the PawPoller
  window opens.

**Step 4. Start the setup.**
The window says **Welcome to PawPoller**. Click **Get Started**.

**Step 5. Choose how you're running it.**
The page asks **"How are you running PawPoller?"** Click **Just on this computer**, then **Next**.

**Step 6. Tell it where your stories are (writers only).**
The page is **Story Archive Location**.

- If you write stories, type the folder where they live (or where you'd like them to live), for
  example `C:\Stories`, and click **Next**.
- If you only post artwork, click **Skip**.

**Step 7. Skip the platforms for now.**
The page is **Connect Your Platforms**. Click **Skip for now**. You'll connect your sites
properly in [Connecting your sites](#connecting-your-sites), one at a time.

**Step 8. Name your persona.**
A persona is the identity your accounts belong to: your fursona or pen name. Type it and click
**Create persona**. (You can click **Skip** and add one later.)

**Step 9. Decide about error reports.**
The page asks whether to **send technical problems to the tech centre**. It lists exactly what is
sent and what never is. Choose **Yes, send them** or **No thanks**. You can change this later.

**Step 10. You're in.**

- **You'll see:** the PawPoller dashboard.

Now go to [Connecting your sites](#connecting-your-sites).

**Keeping it running.** PawPoller only collects stats while it's open. Two settings help, both
under **Settings → Preferences**:

- **Start with Windows**: opens PawPoller every time you log in.
- **Minimize to system tray on close**: closing the window hides PawPoller next to the clock
  instead of quitting, so it keeps working. To quit properly, right-click the PawPoller icon by
  the clock and choose **Quit**.

**Updates** happen by themselves. When you open PawPoller it checks for a newer version first,
installs it, and then opens.

### A2. Linux

**Step 1. Download the app.**
From the [downloads page](https://github.com/knaughtykat01-prog/pawpoller-public/releases/latest),
under **Assets**, download the file ending in `x86_64.AppImage`.

**Step 2. Allow it to run.**
In your file manager, right-click the file → **Properties** → **Permissions** → tick
**Allow executing file as program**. (Or in a Terminal: `chmod +x PawPoller-*.AppImage`.)

**Step 3. Open it.**
Double-click the file.

- **You'll see:** the **Welcome to PawPoller** window.
- **If it goes wrong:** nothing happens on an older Linux (Ubuntu 20.04, Debian 11 or earlier).
  The app needs Ubuntu 22.04, Debian 12, Fedora 37 or newer.

**Step 4 onwards.** Follow Steps 4 to 10 of [A1](#a1-windows) above. They are the same.

To start PawPoller when you log in, turn on **Settings → Preferences → Start with Windows**. The
name says Windows, but it works on Linux too.

---

## Path B: always on, on a computer you own

PawPoller is installed as a **server** on a computer you already have, so it runs in the
background from the moment that computer switches on. No window needs to be open, and it
updates itself. Then **Tailscale** lets you open it from your phone, wherever you are.

**This can be the computer you use every day.** The only rule is that it has to be on (awake)
for PawPoller to work.

**Before you start, check:**

- [ ] The computer is one you can install things on (you know its administrator password).
- [ ] You're happy to leave it switched on, or at least not asleep, most of the time.
- [ ] If the PawPoller **desktop app** is already installed on this computer, quit it (right-click
  its icon by the clock → **Quit**) and turn off its **Start with Windows** setting. The desktop
  app and the server can't both use the same connection. Want to keep both? See
  [Using the desktop app as well](#extra-you-already-use-the-desktop-app) first.

The steps below are for **Windows**. On a Mac or Linux, see
[Mac and Linux: what's different](#mac-and-linux-whats-different) as you go.

**Step B1. Open PowerShell as administrator.**
Click the **Start** button and type `PowerShell`. In the results, right-click
**Windows PowerShell** and choose **Run as administrator**. Click **Yes** when Windows asks.

- **You'll see:** a dark blue or black window. The title bar starts with **Administrator**.

**Step B2. Install the PawPoller server.**
Copy this whole line, paste it into PowerShell (right-click pastes), and press Enter:

```powershell
irm https://raw.githubusercontent.com/knaughtykat01-prog/pawpoller-public/main/installer/server/install.ps1 | iex
```

It downloads PawPoller, checks the download is genuine, installs it, and starts it. This takes a
minute or two.

- **You'll see:** at the end, a line that says **Dashboard (this machine): http://127.0.0.1:8420**.
- **If it goes wrong:** "Run this from an elevated (Administrator) PowerShell" means Step B1
  was missed. Close the window and start again from B1.

**Step B3. Open PawPoller on this computer.**
Open your web browser on **this** computer and go to:

```
http://127.0.0.1:8420
```

- **You'll see:** **Welcome to PawPoller**. Work through the setup the same way as Steps 4 to 10
  of Path A ([A1](#a1-windows)), except there is no "How are you running it?" question.

**Step B4. Set your password. Do this before anything else.**
Right now PawPoller has no password. In PawPoller, go to **Settings → Security**. Near the top it
says **No dashboard password is set**. Click **Set up a password →**. Choose a username and a
password of at least 8 characters, type it twice, and click **Create Account**. Then log in with it.

- **Why this step comes first:** in the next steps you'll make PawPoller reachable from your
  phone. It must have a password before that.
- **If it goes wrong:** "First-time password setup must be done from the machine running
  PawPoller" means the browser isn't on this computer, or the address isn't exactly
  `http://127.0.0.1:8420`. For your safety, the first password can only be set from the computer
  PawPoller runs on.

**Step B5. Stop the computer from going to sleep.**
PawPoller pauses while the computer sleeps. The screen can still turn off; that's fine.

- **Windows 11:** Settings → System → Power & battery → Screen and sleep. Set the option for
  putting the device to sleep **when plugged in** to **Never**.
- **Windows 10:** Settings → System → Power & sleep → under **Sleep**, set **When plugged in** to
  **Never**.

Restarts are fine. PawPoller starts again by itself when Windows starts, even before you log in.

**Step B6. Install Tailscale on this computer.**
Go to [tailscale.com/download](https://tailscale.com/download), download the Windows version, and
install it. When it opens a browser page, sign in with a Google, Microsoft, Apple or GitHub
account. Tailscale's personal plan is free.

- **You'll see:** a Tailscale icon next to the clock.
- **Remember** which account you signed in with. Your phone must use the same one.

**Step B7. Give PawPoller its private address.**
In the administrator PowerShell window from Step B1, paste this and press Enter:

```powershell
tailscale serve --bg 8420
```

- **You'll see:** your PawPoller address. It looks like `https://your-pc-name.tail1234.ts.net`.
  Write it down.
- **If it goes wrong:** if it shows a link and says the feature isn't enabled, open that link,
  click to enable it, then run the same command again.

This address only works on devices signed in to your Tailscale account. To everyone else on the
internet, it doesn't exist.

**Step B8. Install Tailscale on your phone.**
Get **Tailscale** from the App Store (iPhone) or Google Play (Android). Open it and sign in with
the **same account** as Step B6. Allow it to add a VPN when your phone asks: that is how
Tailscale makes the private connection.

**Step B9. Open PawPoller on your phone.**
In your phone's browser, go to the address from Step B7 and log in with your password from B4.

- **You'll see:** your PawPoller dashboard, sized for the phone.
- **If it goes wrong:** if the page won't load, open the Tailscale app on the phone and check
  it says **Connected**.

**Step B10. Add it to your home screen (optional).**
PawPoller can sit on your phone like an app.

- **iPhone or iPad:** open the address in **Safari** (this only works in Safari), tap **Share**,
  then **Add to Home Screen**.
- **Android:** in Chrome, tap **⋮** then **Add to Home screen** (or **Install app**).

**That's it.** Now go to [Connecting your sites](#connecting-your-sites).

**Updates** install themselves: the server checks for a new version once a day. You don't need to
do anything.

### Mac and Linux: what's different

The steps are the same, with these changes:

- **B1:** open **Terminal** instead of PowerShell. (Mac: press Cmd+Space, type `Terminal`.)
- **B2:** use this command instead. It will ask for your computer's password; type it (nothing
  appears as you type) and press Enter.

  ```bash
  curl -fsSL https://raw.githubusercontent.com/knaughtykat01-prog/pawpoller-public/main/installer/server/install.sh | bash
  ```

- **B5:** Mac: System Settings → Energy (or Battery → Options) → turn on **Prevent automatic
  sleeping when the display is off**. Linux: in your desktop's Power settings, set automatic
  suspend to **Off** when plugged in.
- **B7:** on Linux, put `sudo` at the front: `sudo tailscale serve --bg 8420`. On a Mac, the
  `tailscale` command needs Tailscale's command-line tool. Tailscale's own
  [command-line guide](https://tailscale.com/kb/1080/cli) shows how to set it up.

### Extra: using PawPoller without the Tailscale app on your phone

If you'd rather not install Tailscale on your phone, Tailscale can give your address a **public**
version that opens in any browser. The trade-off: anyone on the internet can then reach your
login page. Your password and a second login step keep them out, so both are required.

1. Make sure you've done **Step B4** (password).
2. **Turn on two-factor login.** In PawPoller: **Settings → Security → Two-Factor
   Authentication**. Scan the code with an authenticator app on your phone (Google
   Authenticator, Microsoft Authenticator and Aegis all work). Write down the **recovery codes**
   it shows you and keep them somewhere safe. They are shown once.
3. In the administrator PowerShell window, run:

   ```powershell
   tailscale funnel --bg 8420
   ```

   If it shows a link and says the feature isn't enabled, open the link, enable it, and run the
   command again.
4. The address from Step B7 now opens in any browser, on any device.

To make it private again, run `tailscale serve reset`, then run the Step B7 command again.

Own a domain name? [Use a web address you already own](#use-a-web-address-you-already-own)
gives you `https://pawpoller.yourdomain.com` instead.

### Extra: you already use the desktop app

The desktop app and the server both use the same connection number (called a *port*, `8420`), so on
one computer they collide. Give the server a different number **before** you install it — and if you
already have a library in the desktop app, bring it across.

**1. Give the server its own port.**
In Step B2, run this line first, then the install command:

```powershell
$env:PAWPOLLER_PORT = '8421'
```

Everywhere this guide says `8420` for the server (Steps B3 and B7), use `8421` instead.

**2. Move what's already in the desktop app.**
Skip this if you'd rather start the server empty. Do it straight after Step B3 and **before** you
set a password, because your desktop copy brings its own settings with it.

1. **Quit the desktop app completely.** Close the window, then look in the clock corner of the
   taskbar for the PawPoller icon and quit it there too. Nothing should be running.
2. In the administrator PowerShell window, pause the server:

   ```powershell
   Stop-ScheduledTask -TaskName 'PawPoller Server'
   ```

3. Copy your library, your settings and your saved site logins over:

   ```powershell
   Copy-Item "$env:APPDATA\PawPoller\data\*" "C:\ProgramData\PawPoller-Server\data\data\" -Recurse -Force
   ```

   (The second `data` is not a typo — the server keeps its files in a `data` folder inside its own
   folder.)

4. Start the server again:

   ```powershell
   Start-ScheduledTask -TaskName 'PawPoller Server'
   ```

- **You'll see:** open `http://127.0.0.1:8421` and your own pieces, accounts and history are
  there.
- **Your desktop copy is left exactly where it was**, so nothing is lost if you change your mind.
- **If it goes wrong:** "Access to the path is denied" means something is still running. Check the
  desktop app is quit and that Step 2 above finished, then run the copy again.

Now carry on with Step B4 (set a password) and the rest of Path B.

**3. Point the desktop app at the server.**
Open the desktop app, go to **Settings**, click **Re-run setup**, and choose **Connect to my
server**. Enter `http://127.0.0.1:8421` as the server address, and an API key from the server: in
the server's dashboard, **Settings → Security → API Keys → generate**.

The desktop app then becomes a window onto your server, and helps with the few things a browser
can't do, such as signing in to some sites for you. Only the server checks your sites and posts
from now on, so nothing happens twice.
---

## Path C: a rented server

You rent a small computer in a data centre (a "cloud server") and PawPoller runs there around
the clock. This path involves more typing than the others, all of it copy and paste. If Path B
would work for you, it is simpler.

**Cost:** about US$6 a month. **Time:** about an hour, much of it waiting.

The steps use **DigitalOcean**, which has the friendliest setup. Other hosts work too: see
[Other cloud hosts](#other-cloud-hosts).

**Step C1. Make a key to log in with.**
Cloud servers are safest when you log in with a *key* (a pair of files on your computer) instead
of a password. On your own computer, open **PowerShell** (Windows) or **Terminal** (Mac, Linux)
and run:

```bash
ssh-keygen -t ed25519 -C "pawpoller"
```

Press Enter three times to accept the defaults. Then show the key you'll give the host:

```bash
cat ~/.ssh/id_ed25519.pub
```

(On Windows PowerShell use: `type $env:USERPROFILE\.ssh\id_ed25519.pub`)

- **You'll see:** one long line starting `ssh-ed25519`. Keep this window open. You'll copy that
  line in the next step.
- **Only ever share the file ending in `.pub`.** The other one is your private key and never
  leaves your computer.

**Step C2. Create the server.**

1. Sign up at [digitalocean.com](https://www.digitalocean.com/) and add a payment method.
2. Click **Create → Droplets**.
3. **Region:** the one closest to you.
4. **Image:** Ubuntu 24.04 (LTS) x64.
5. **Size:** Basic → Regular → the **$6/mo** option (1 GB RAM). That is plenty.
6. **Authentication:** choose **SSH Key** → **New SSH Key**, and paste the line from Step C1.
7. **Hostname:** `pawpoller`.
8. Click **Create Droplet**.

- **You'll see:** after about a minute, an **IP address** such as `203.0.113.42`. Write it down.

**Step C3. Log in to the server.**
In the same PowerShell or Terminal window, run this, replacing the numbers with your server's IP
address:

```bash
ssh root@203.0.113.42
```

Type `yes` if it asks whether to continue connecting.

- **You'll see:** the prompt changes to something like `root@pawpoller:~#`. You are now typing
  on the server. Every command from here runs there.
- **If it goes wrong:** "Permission denied (publickey)" means the key pasted in Step C2 doesn't
  match this computer's key. Repeat C1 and C2 carefully.

**Step C4. Install Docker (the tool that runs PawPoller).**
Copy this whole block, paste it, and press Enter:

```bash
apt update && apt install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

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
Still on the server, install Tailscale and connect it to your account:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

`tailscale up` prints a link. Open it on your own computer and sign in with the account you'll use
on your phone. Then, back on the server:

```bash
tailscale serve --bg 8420
```

- **You'll see:** your address, like `https://pawpoller.tail1234.ts.net`. Write it down.
- **If it goes wrong:** if it shows a link and says the feature isn't enabled, open the link,
  enable it, and run the command again.

**Step C10. Install Tailscale on your phone and your computer.**
Install Tailscale on each device you'll use PawPoller from, signed in with the **same account**
(see Steps [B6](#path-b-always-on-on-a-computer-you-own) and B8 for how).

**Step C11. Open PawPoller and finish setup.**
On your phone or computer, open the address from Step C9 and log in with `admin` and the password
from Step C6.

- **You'll see:** **Welcome to PawPoller**. Work through it like Steps 4 to 10 of
  [Path A](#a1-windows).

You can add PawPoller to your phone's home screen (see Step B10). Then go to
[Connecting your sites](#connecting-your-sites).

Want your own web address instead of the Tailscale one? See
[Use a web address you already own](#use-a-web-address-you-already-own).

**Updates:** log in to the server (Step C3), then run:

```bash
cd pawpoller-public
./update.sh
```

Prefer a button? [Update from the dashboard](#update-from-the-dashboard-docker) sets up an
**Update now** button, or daily automatic updates.

---

## Connecting your sites

This is the same on every path.

**Step 1. Start with one site.** Pick the site you use most. Get it working before adding more.

**Step 2. Open it in Settings.** Go to **Settings → Platforms** and click the site.

**Step 3. Follow its guide.** Click **📖 Setup guide** on the site's card. Each guide has pictures
and shows exactly where to find what the site needs. (Every guide is also on the **Getting
Started** page in the side menu, and each one can be opened as a PDF to print.)

**Step 4. Test it.** Open the **Accounts** page from the side menu and press **Test** on that
site's account.

- **You'll see:** which account it's logged in as, for example "Logged in as YourName". Check
  it's the account you meant. This matters if you have more than one account on a site.

**Step 5. Repeat** for the next site.

> **Reading and posting are different permissions.** Some sites give you a key that can read your
> stats but not post. PawPoller will poll happily and then every post fails. The table below says
> which sites need something extra to post.

You don't need to connect all twenty. Connect what you use.

---

## Platform credentials

**Twenty platforms.** Nineteen can be polled for stats, seventeen can be posted to, and nine can be
edited in place after posting. You only need credentials for the ones you use.

Add them one at a time in **Settings → Platforms**, then press that account's **Test** button on
the **Accounts** page before moving on. (To paste renewed cookies or tokens for one account, use
its **🔑 Credentials** button on the same page.)

### What each platform needs

| Platform | To poll stats | To post & edit | Where to get it |
|---|---|---|---|
| **Inkbunny** | Username + password | *same* | Your IB login — then tick **Enable API access** in IB's account settings, or nothing will work |
| **FurAffinity** | Username + `a` and `b` cookies | *same* | Log in to FA, then DevTools → Application → Cookies → `furaffinity.net` → copy `a` and `b`. On the desktop app, **Login via Browser** captures them for you |
| **SoFurry** | Personal Access Token | *same* | sofurry.com → Settings → Developer → New token ([direct link](https://sofurry.com/settings/pat-create)). **No password needed**, and 2FA accounts work fine — a token never logs in. Your handle is read from the token |
| **Weasyl** | API key | *same* | weasyl.com → Settings → API Keys → generate. The username is discovered from the key |
| **AO3** | Username + password | *same* | Your AO3 login. The account you log in with can differ from the one whose works you track |
| **SquidgeWorld** | Username + password | *same* | Your SqW login. Same split as AO3 — logging in and posting can be different accounts |
| **DeviantArt** | App `client_id` + `client_secret` | **plus** a one-time **Authorise posting** | deviantart.com/developers/apps → Register Application (Client type: **Confidential**) → copy both values, and enter the DA username to track. Then **Authorise posting** on the account. ⚠ See [DeviantArt: authorise from the right account](#deviantart-authorise-from-the-right-account) — this step is easy to get wrong |
| **e621** | Username + API key | *same* | e621 → Account → **Manage API Access**. This is an API key, **not** your password |
| **Itaku** | Handle only — no login | **plus** an auth token | Polling a public gallery needs nothing at all. To post or edit, log in at itaku.ee and copy the auth token from your browser session |
| **FurryNetwork** | Refresh token | *same* | ⚠ **Email + password no longer works** — FN put its password login behind a CAPTCHA in August 2026. Log in at furrynetwork.com, then DevTools → Application → copy the `refresh_token` out of the stored session |
| **Bluesky** | Handle + app password | *same* | bsky.app → Settings → App Passwords → generate. Use the app password, never your real one |
| **Mastodon** | Instance URL + access token | **token needs `write:statuses` + `write:media`** | Your instance → Preferences → Development → New application. ⚠ A read-only token polls fine and then fails every post |
| **X / Twitter** | `auth_token` + `ct0` cookies | *same* | DevTools → Application → Cookies → `x.com` → copy both. ⚠ See [Credentials that expire](#credentials-that-expire) — X allows one session per browser |
| **Tumblr** | OAuth consumer key + blog name | **plus** consumer secret, OAuth token and token secret | tumblr.com/oauth/apps → register an app. Polling needs only the "OAuth Consumer Key"; posting needs the full OAuth 1.0a set |
| **Threads** | Token with `threads_basic` (+ `threads_manage_insights`) | — (analytics only) | A Meta app at developers.facebook.com — the same one as Instagram. **[Step-by-step with screenshots: THREADS_SETUP.md](THREADS_SETUP.md)**. Long-lived tokens last ~60 days, and PawPoller extends them for you |
| **Instagram** | Token with `instagram_business_basic` + `instagram_business_manage_insights` | **plus `instagram_business_content_publish`** | The most involved of the twenty — **[full step-by-step: INSTAGRAM_SETUP.md](INSTAGRAM_SETUP.md)**. In short: a Meta app, and a **Business or Creator** account (a personal one cannot be used at all). ⚠ Do the setup in **Edge or Firefox, not Chrome** — Chrome breaks Meta's token generator silently |
| **Telegram** | *not polled* | Its own bot token + channel (never the notification bot, since 4.8.0) | **[Full step-by-step: TELEGRAM_SETUP.md](TELEGRAM_SETUP.md)**. Create a bot with [@BotFather](https://t.me/BotFather), then **make the bot an admin of your channel and tick “Post Messages”** — admin rights are individual toggles and that one is the usual failure. A **private** channel has no `@username`: use 🔍 **Find my channel** and PawPoller fetches its numeric id for you |
| **Pixiv** | Refresh token | *poll only* | A one-time browser login via a helper such as `gppt` |
| **Wattpad** | Public handle | *poll only* | Nothing to set up — just the username |
| **Furbooru** | Public handle | *poll only* | Nothing needed to read. An optional API key (furbooru.org → Account → API key) raises your rate limit |

### DeviantArt: authorise from the right account

DeviantArt needs **two** credentials, and only the first is obvious:

1. The **app** credentials (`client_id` + `client_secret`) let PawPoller read public data. Polling works with just these.
2. A **per-account authorisation** decides which account a post lands on. Without it, posting fails.

The trap: DA's consent screen authorises **whichever account that browser is currently signed in to**, not the one whose button you pressed. Approving from the wrong session stores the wrong account's permission, reports success, and every post afterwards goes to the wrong gallery.

**So: open a private window, sign in as the account you want, and only then press Authorise posting.** PawPoller checks who actually approved and refuses a mismatch rather than storing it, but it is much easier to get right the first time than to unpick afterwards.

### Credentials that expire

Most do not. Tokens, API keys and app passwords (SoFurry, Weasyl, e621, Bluesky, Mastodon, Tumblr) keep working until you revoke them, and Instagram and Threads refresh themselves.

Three are browser sessions with no refresh path, so PawPoller warns you as they age rather than waiting for something to fail:

| Platform | Typical life |
|---|---|
| X / Twitter | ~30 days |
| FurAffinity | ~45 days |
| DeviantArt cookie | ~45 days |

**One session per browser** applies to FurAffinity and X. Renewing several accounts in the same browser leaves every account except the last holding a stale cookie. Use a fresh private window per account, and save each one before signing in as the next.

---

## Keeping it healthy

### Updates

| Path | How it updates |
|---|---|
| A: desktop app | By itself, each time you open it. To turn that off: **Settings → About & updates**. |
| B: your own computer | By itself, once a day. Nothing to do. |
| C: rented server | Run `./update.sh` on the server (Step C11), or set up the [Update now button](#update-from-the-dashboard-docker). |

### Backups

The simplest backup is built in: **Settings → Data & backups → Backup & Restore** downloads one
`.zip` file with your stats, settings, saved logins (still encrypted) and media. Make one before
big changes, and keep it somewhere other than the computer PawPoller runs on.

> **Path C:** your backup's saved logins can only be unlocked with the vault key from Step C7.
> Keep the key and the backups separately, and keep both.

### Where your data lives

| Path | Folder |
|---|---|
| A: Windows | `%APPDATA%\PawPoller` (the same as `C:\Users\<you>\AppData\Roaming\PawPoller`) |
| A: Linux | `~/.local/share/PawPoller` |
| B: Windows | `C:\ProgramData\PawPoller-Server\data` |
| B: Linux | `/var/lib/pawpoller` |
| B: Mac | `~/Library/Application Support/PawPoller-Server/data` |
| C: rented server | The Docker volume `pawpoller-data` (inside it: `/app/data`) |

---

## When something goes wrong

**"Port 8420 already in use" / the server won't start.**
Something else is using PawPoller's connection number. Usually it's the desktop app and the server
on the same computer. Quit the desktop app, or see
[Using the desktop app as well](#extra-you-already-use-the-desktop-app).

**A site stopped updating.**
Go to **Settings → Platforms** and look for a red or amber dot. The usual cause is an expired
login: sites that use browser cookies (FurAffinity, X, DeviantArt) need them re-pasting every few
weeks. PawPoller warns you before they expire. See [Credentials that expire](#credentials-that-expire).

**"Unverified" next to a site.**
PawPoller couldn't check that site just now, often because the site itself is having a moment.
That is not the same as your login being wrong. Wait a while, then press **Check sessions now**
(**Settings → Platforms**).

**Posts fail on a site that polls fine.**
Your key can read but not post. Check that site's row in [Platform credentials](#platform-credentials).

**I forgot my password.**

- **Path C:** log in to the server (Step C3), go into the folder (`cd pawpoller-public`), and run
  this with your new password in place of `NEW-PASSWORD`:

  ```bash
  docker compose -f docker-compose.image.yml exec pawpoller python -c "import config; config.save_settings({'auth_username': 'admin', 'auth_password_hash': config.hash_password('NEW-PASSWORD')})"
  ```

  Log in with `admin` and the new password straight away; no restart is needed. (Changing
  `DASHBOARD_PASSWORD` in `.env` does **not** work once a password exists. That line is only read
  the very first time.)
- **Path A (the app on your computer):** close PawPoller, then open a Command Prompt and run the
  app with `--reset-password`:

  ```
  "C:\Program Files\PawPoller\PawPoller.exe" --reset-password
  ```

  It asks for the new password twice, then closes. Start PawPoller normally and sign in with it.

- **Path B (your own computer as a server):** the same, using the server program:

  ```
  "C:\Program Files\PawPoller\PawPoller-Server.exe" --reset-password
  ```

  You'll see *Password changed.* Start it again and sign in.

  You have to be sitting at that computer — this only works from its own keyboard, never over the
  network. It never asks for your old password, so a forgotten one is not a problem.

**The phone can't open my address (Paths B and C).**
Open the Tailscale app on the phone and check it says **Connected**, and that it's signed in to the
same account as the computer or server.

**A site works on my computer but not on my rented server.**
A few sites block data-centre connections. PawPoller can route those through a Cloudflare Worker:
see the `CF_WORKER_URL` block in `.env.example`.

**PDFs come out blank on Linux or a server.**
Some system libraries are missing. On Ubuntu or Debian:
`sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0`, then restart PawPoller.

**My stories disappeared after starting PawPoller on a server.**
The story folder wasn't set, so PawPoller is looking at an empty one. See
[Story folder on a server](#story-folder-on-a-server).

---

# Part 2: advanced

Everything below is optional. You won't need it for Paths A, B or C.

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
   [Using the desktop app as well](#extra-you-already-use-the-desktop-app)).
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
from [Step 5](#use-a-web-address-you-already-own) as well.

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
tracked file. Under the hood it runs `git pull --ff-only`, then either
`docker compose up -d --build` (built from source) or `docker compose -f docker-compose.image.yml pull`
and `up -d` (prebuilt image).

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

More detail: [security/SELF_HOST_SECURITY.md](security/SELF_HOST_SECURITY.md).

## Installing without the installer (portable zip)

1. From the [downloads page](https://github.com/knaughtykat01-prog/pawpoller-public/releases/latest),
   download `PawPoller-windows-x64.zip`.
2. Right-click it → **Properties** → tick **Unblock** → **OK**. Then extract it anywhere, for
   example `C:\PawPoller`.
3. Double-click `PawPoller.exe`.

To update, extract a newer zip over the old folder. Your data is stored separately and isn't touched.

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

## Uninstalling

**Desktop app, Windows:** Start → type `PawPoller` → right-click → **Uninstall** (or Settings →
Apps). It offers to keep your data folder so a reinstall picks up where you left off.

**Portable zip or Linux AppImage:** in PawPoller, **Settings → About & updates → Danger zone →
Uninstall PawPoller**. Tick what to remove (the app, your data, the start-up entry), type
`UNINSTALL`, and confirm.

By hand on Linux:

```bash
rm -f /path/to/PawPoller-*.AppImage
rm -rf ~/.local/share/PawPoller
rm -f ~/.config/autostart/PawPoller.desktop
```

By hand for the Windows zip (PowerShell):

```powershell
Remove-Item -Recurse -Force "C:\Path\To\PawPoller"
Remove-Item -Recurse -Force "$env:APPDATA\PawPoller"          # your data
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "PawPoller" -ErrorAction SilentlyContinue
cmdkey /delete:PawPoller
```

## Where to go next

- [Releases](https://github.com/knaughtykat01-prog/pawpoller-public/releases): what changed in each version.
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md): working on PawPoller's code.
- [`ROADMAP_PUBLIC.md`](ROADMAP_PUBLIC.md): what's planned.

If something here is missing or wrong, open an issue on the project page.
