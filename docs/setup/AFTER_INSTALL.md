# After you install

You've installed PawPoller with one of the three paths. This guide is the rest, and it is the
same for all of them: finishing setup, connecting your sites, keeping PawPoller healthy, and what
to do when something goes wrong. Where a path does something differently, it says so.

Not installed yet? [Start here](../SETUP.md#start-here-which-of-these-are-you).

---

## Open PawPoller and finish setup

**Path A (the desktop app)** and **Path B (a computer you own)** already did this, in Steps 4
to 10 of [A1](DESKTOP.md#a1-windows) and in [Step B3](HOME_SERVER.md). Go straight to
[Connecting your sites](#connecting-your-sites).

**Path C (a rented server): Step C11. Open PawPoller and finish setup.**
On your phone or computer, open the address from [Step C9](RENTED_SERVER.md) and log in with
`admin` and the password from Step C6.

- **You'll see:** **Welcome to PawPoller**. Work through it like Steps 4 to 10 of
  [Path A](DESKTOP.md#a1-windows).

You can add PawPoller to your phone's home screen (see [Step B10](HOME_SERVER.md)). Then go to
[Connecting your sites](#connecting-your-sites).

Want your own web address instead of the Tailscale one? See
[Use a web address you already own](ADVANCED.md#use-a-web-address-you-already-own).

---

## Connecting your sites

This is the same on every path.

**Step 1. Start with one site.** Pick the site you use most. Get it working before adding more.

**Step 2. Open it in Settings.** Go to **Settings → Platforms** and click the site.

**Step 3. Sign in, if the site offers it.** FurAffinity, X, Newgrounds, Itaku and a few others
have a **Sign in** button.

- **On the desktop app:** press **Sign in**. The site's own sign-in page opens in a PawPoller
  window. Sign in the way you always do. The window closes by itself when you're in.
- **On a server or a hosted seat:** type your username, then press **Sign in with the PawPoller
  app**. A six-character code appears, good for ten minutes. On your computer, open the PawPoller
  desktop app, go to **Accounts → Sign in for a server** (or the tray icon's menu), and type the
  server's address and the code. A box asks you to confirm, then the site's sign-in page opens.
  Sign in there. **You'll see:** "Connected as YourName ✓" on the server.
- **No Sign in button, or it didn't work?** Open **Other ways to connect** under the site and
  follow its guide (next step).

**Step 3b. Or follow its guide.** Click **📖 Setup guide** on the site's card. Each guide has pictures
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

### Already use PostyBirb?

PawPoller can bring your PostyBirb submissions in as Library pieces (file, title, description, tags and rating), and
copy the site logins PostyBirb has. PostyBirb itself is never changed. Run it any time; anything already in PawPoller
is skipped.

**On the desktop app:**

1. Close PostyBirb (it holds its files open while it runs).
2. Go to **Settings → Data → Bring in from PostyBirb** and press **Bring in**.
3. **Library:** tick the submissions you want (or **Tick all**), then press **Bring them in**. You'll see each one
   listed as added or skipped, with the reason.
4. **Logins:** open the **Logins** tab. Nothing is ticked for you: tick only the accounts that are yours, then press
   **Bring them over**.

Your PostyBirb **description templates** and **tag groups** come along with the Library step (the box **Also bring
in … saved descriptions and … tag groups** is ticked for you; untick it to leave them). Afterwards:

- In a piece's description box, press **📝 Saved description** and choose one. It goes in where your cursor is.
- By a piece's tags, press **➕ Tag group** and choose one. Its tags are added; ones already there are skipped.
- To rename, change or delete them, go to **Settings → Saved text**. You can also make new ones there.

**On a server or a hosted seat** the server can't see your computer, so you send it a zip of PostyBirb's folder:

1. On your computer, zip the **PostyBirb** folder from **Documents** (Windows: right-click it, **Send to →
   Compressed (zipped) folder**). The steps for Mac and Linux are on the screen.
2. For your logins too, zip PostyBirb's **Partitions** folder as well (Windows: `%APPDATA%\postybirb-plus`).
3. In PawPoller, go to **Settings → Data → Bring in from PostyBirb**, press **Bring in** and choose the zip (or both
   zips). You'll see the upload's progress, then the same Library and Logins lists as on the desktop.

The zip holds your site logins. It only goes over a secure connection (https, Tailscale, or the server itself), is
read once, and is deleted as soon as you finish or close the window. Left unused for 30 minutes, it can't be used
any more and is deleted soon after (within the hour).

---

## Platform credentials

**Twenty platforms.** Nineteen can be polled for stats, seventeen can be posted to, and nine can be
edited in place after posting. You only need credentials for the ones you use.

Add them one at a time in **Settings → Platforms**, then press that account's **Test** button on
the **Accounts** page before moving on. (To paste renewed cookies or tokens for one account, use
its **🔑 Sign in** button on the same page.)

### What each platform needs

| Platform | To poll stats | To post & edit | Where to get it |
|---|---|---|---|
| **Inkbunny** | Username + password | *same* | Your IB login — then tick **Enable API access** in IB's account settings, or nothing will work |
| **FurAffinity** | Username + `a` and `b` cookies | *same* | Log in to FA, then DevTools → Application (Firefox: Storage) → Cookies → `furaffinity.net` → copy `a` and `b`. Easiest: press **Sign in** (desktop app) or **Sign in with the PawPoller app** (server or seat) |
| **SoFurry** | Personal Access Token | *same* | sofurry.com → Settings → Developer → New token ([direct link](https://sofurry.com/settings/pat-create)). **No password needed**, and 2FA accounts work fine — a token never logs in. Your handle is read from the token |
| **Weasyl** | API key | *same* | weasyl.com → Settings → API Keys → generate. The username is discovered from the key |
| **AO3** | Username + password | *same* | Your AO3 login. The account you log in with can differ from the one whose works you track |
| **SquidgeWorld** | Username + password | *same* | Your SqW login. Same split as AO3 — logging in and posting can be different accounts |
| **DeviantArt** | App `client_id` + `client_secret` | **plus** a one-time **Authorise posting** | deviantart.com/developers/apps → Register Application (Client type: **Confidential**) → copy both values, and enter the DA username to track. Then **Authorise posting** on the account. ⚠ See [DeviantArt: authorise from the right account](#deviantart-authorise-from-the-right-account) — this step is easy to get wrong |
| **e621** | Username + API key | *same* | e621 → Account → **Manage API Access**. This is an API key, **not** your password |
| **Itaku** | Handle only — no login | **plus** an auth token | Polling a public gallery needs nothing at all. To post or edit, press **Sign in** (or **Sign in with the PawPoller app** on a server), or copy the auth token from your browser session |
| **FurryNetwork** | Refresh token | *same* | ⚠ **Email + password no longer works** — FN put its password login behind a CAPTCHA in August 2026. Log in at furrynetwork.com, then DevTools → Application → copy the `refresh_token` out of the stored session |
| **Bluesky** | Handle + app password | *same* | bsky.app → Settings → App Passwords → generate. Use the app password, never your real one |
| **Mastodon** | Instance URL + access token | **token needs `write:statuses` + `write:media`** | Your instance → Preferences → Development → New application. ⚠ A read-only token polls fine and then fails every post |
| **X / Twitter** | `auth_token` + `ct0` cookies | *same* | Easiest: press **Sign in**, as for FurAffinity. Or DevTools → Application (Firefox: Storage) → Cookies → `x.com` → copy both. ⚠ See [Credentials that expire](#credentials-that-expire) — X allows one session per browser |
| **Tumblr** | OAuth consumer key + blog name | **plus** consumer secret, OAuth token and token secret | tumblr.com/oauth/apps → register an app. Polling needs only the "OAuth Consumer Key"; posting needs the full OAuth 1.0a set |
| **Threads** | Token with `threads_basic` (+ `threads_manage_insights`) | — (analytics only) | A Meta app at developers.facebook.com — the same one as Instagram. **[Step-by-step with screenshots: THREADS_SETUP.md](../THREADS_SETUP.md)**. Long-lived tokens last ~60 days, and PawPoller extends them for you |
| **Instagram** | Token with `instagram_business_basic` + `instagram_business_manage_insights` | **plus `instagram_business_content_publish`** | The most involved of the twenty — **[full step-by-step: INSTAGRAM_SETUP.md](../INSTAGRAM_SETUP.md)**. In short: a Meta app, and a **Business or Creator** account (a personal one cannot be used at all). ⚠ Do the setup in **Edge or Firefox, not Chrome** — Chrome breaks Meta's token generator silently |
| **Telegram** | *not polled* | Its own bot token + channel (never the notification bot, since 4.8.0) | **[Full step-by-step: TELEGRAM_SETUP.md](../TELEGRAM_SETUP.md)**. Create a bot with [@BotFather](https://t.me/BotFather), then **make the bot an admin of your channel and tick “Post Messages”** — admin rights are individual toggles and that one is the usual failure. A **private** channel has no `@username`: use 🔍 **Find my channel** and PawPoller fetches its numeric id for you |
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
| C: rented server | Run `./update.sh` on the server ([Updates](RENTED_SERVER.md#updates)), or set up the [Update now button](RENTED_SERVER.md#update-from-the-dashboard-docker). |

### Backups

The simplest backup is built in: **Settings → Data & backups → Backup & Restore** downloads one
`.zip` file with your stats, settings, saved logins (still encrypted) and media. Make one before
big changes, and keep it somewhere other than the computer PawPoller runs on.

> **Path C:** your backup's saved logins can only be unlocked with the vault key from [Step C7](RENTED_SERVER.md).
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
[Using the desktop app as well](HOME_SERVER.md#extra-you-already-use-the-desktop-app).

**The desktop app can't reach your server, or says the address "isn't a PawPoller any more".**
If your PawPoller has moved (to a hosted seat, or a new address at home), point the desktop app at
its new address.

1. On your server, open **Settings → Security → API Keys** and make a new key. Copy it.
2. In the desktop app, click **Change server…**. It's on the "can't reach" page, and in the
   PawPoller icon's menu in the clock corner of the taskbar. A small window opens.
3. Type your server's new address (it starts with `https://`) and click **Check**.
   **You'll see:** "Move this app from … to …?" with both addresses spelled out.
4. Make sure the second address is really your server, then click **Yes, that's my server**.
5. Paste the key from step 1 and click **Connect**.
   **You'll see:** "Connected", and the app opens your server.
6. If your old server still runs, remove this computer's old key there (**Settings → Security →
   API Keys**).

If anything is wrong (a typo, the wrong key), the window says what, and nothing changes.

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

If your account has a confirmed email, press **Forgot your password?** on the sign-in page and
PawPoller emails you a code. Without an email, set a new password on the machine PawPoller runs on.
The sign-in page shows the same steps.

- **Path A (the app on your computer, Windows):**
  1. Close PawPoller: right-click its icon by the clock and choose **Quit**.
  2. Open the **Start** menu and click **Reset PawPoller password**. A small window opens.
  3. Type your new password and press Enter. Nothing shows while you type; that's on purpose.
  4. Type it again and press Enter. You'll see *Password changed.* Press Enter to close the window.
  5. Open PawPoller and sign in with the new password.

  No shortcut (a portable copy)? Open a Command Prompt and run the app with `--reset-password`:

  ```
  "C:\Program Files\PawPoller\PawPoller.exe" --reset-password
  ```

- **Path B (your own computer as a server):**
  - **Windows:** open the Start menu, type `cmd`, right-click **Command Prompt** and choose
    **Run as administrator**. Then type these one at a time, pressing Enter after each:

    ```
    schtasks /end /tn "PawPoller Server"
    set PAWPOLLER_APPDATA_DIR=C:\ProgramData\PawPoller-Server\data
    "C:\ProgramData\PawPoller-Server\current\PawPoller-Server.exe" --reset-password
    schtasks /run /tn "PawPoller Server"
    ```

    The third one asks for the new password twice and shows nothing while you type.
  - **Linux:** in a terminal, one at a time:

    ```
    sudo systemctl stop pawpoller-server
    sudo -u pawpoller sh -c 'set -a; . /etc/pawpoller-server.env; exec "$PAWPOLLER_SERVER_ROOT/current/PawPoller-Server" --reset-password'
    sudo systemctl start pawpoller-server
    ```

  You'll see *Password changed.* Sign in with the new password.

- **Path C (a server with Docker):**
  1. Log in to the server ([Step C3](RENTED_SERVER.md)) and go into the folder: `cd pawpoller-public`
  2. Stop PawPoller:
     `docker compose -f docker-compose.image.yml stop pawpoller`
  3. Set the new password. It asks twice and shows nothing while you type:
     `docker compose -f docker-compose.image.yml run --rm pawpoller python server.py --reset-password`
  4. Start PawPoller again:
     `docker compose -f docker-compose.image.yml start pawpoller`
  5. Sign in with the new password.

  (Changing `DASHBOARD_PASSWORD` in `.env` does **not** work once a password exists. That line is
  only read the very first time.)

Every way of resetting signs out every browser that was signed in. You have to be at that machine
(or logged in to it): this never works over the network, and it never asks for your old password.
If you think someone else got in, open **Settings → Security → API keys** afterwards and remove any
you don't recognise.

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
[Story folder on a server](RENTED_SERVER.md#story-folder-on-a-server).
