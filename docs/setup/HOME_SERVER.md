# Path B: always on, on a computer you own

PawPoller is installed as a **server** on a computer you already have, so it runs in the
background from the moment that computer switches on. No window needs to be open, and it
updates itself. Then **Tailscale** lets you open it from your phone, wherever you are.

Not sure this is the right guide? [Start here](../SETUP.md#start-here-which-of-these-are-you).
Under most steps you'll find **You'll see:** (what should happen) and **If it goes wrong:** (the
most common problem, and the fix).

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
  of Path A ([A1](DESKTOP.md#a1-windows)), except there is no "How are you running it?" question.

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

**That's it.** Now go to [Connecting your sites](AFTER_INSTALL.md#connecting-your-sites).

**Updates** install themselves: the server checks for a new version once a day. You don't need to
do anything.

---

## Mac and Linux: what's different

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

---

## Extra: using PawPoller without the Tailscale app on your phone

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

Own a domain name? [Use a web address you already own](ADVANCED.md#use-a-web-address-you-already-own)
gives you `https://pawpoller.yourdomain.com` instead.

---

## Extra: you already use the desktop app

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
