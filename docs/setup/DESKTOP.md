# Path A: the desktop app

PawPoller runs as a normal app on your computer. It collects stats and posts while it's open.

Not sure this is the right guide? [Start here](../SETUP.md#start-here-which-of-these-are-you).
Under most steps you'll find **You'll see:** (what should happen) and **If it goes wrong:** (the
most common problem, and the fix).

**What you need:** a Windows 10 or 11 computer, or Linux. (There is no Mac app yet. On a Mac,
follow [Path B](HOME_SERVER.md) instead. It works on a Mac.)

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
  block unrecognised apps. Ask whoever manages the computer, or use the
  [portable version](#installing-without-the-installer-portable-zip) below.

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
properly in [Connecting your sites](AFTER_INSTALL.md#connecting-your-sites), one at a time.

**Step 8. Name your persona.**
A persona is the identity your accounts belong to: your fursona or pen name. Type it and click
**Create persona**. (You can click **Skip** and add one later.)

**Step 9. Decide about error reports.**
The page asks whether to **send technical problems to the tech centre**. It lists exactly what is
sent and what never is. Choose **Yes, send them** or **No thanks**. You can change this later.

**Step 10. You're in.**

- **You'll see:** the PawPoller dashboard.

Now go to [Connecting your sites](AFTER_INSTALL.md#connecting-your-sites).

**Keeping it running.** PawPoller only collects stats while it's open. Two settings help, both
under **Settings → General**:

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

To start PawPoller when you log in, turn on **Settings → General → Start with Windows**. The
name says Windows, but it works on Linux too.

---

## Installing without the installer (portable zip)

1. From the [downloads page](https://github.com/knaughtykat01-prog/pawpoller-public/releases/latest),
   download `PawPoller-windows-x64.zip`.
2. Right-click it → **Properties** → tick **Unblock** → **OK**. Then extract it anywhere, for
   example `C:\PawPoller`.
3. Double-click `PawPoller.exe`.

To update, extract a newer zip over the old folder. Your data is stored separately and isn't touched.

---

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
