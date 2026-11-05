# Getting PawPoller Running

This guide gets PawPoller working for you, one step at a time. There are three ways to run it,
and each has its own guide: pick yours below and follow only that one. Paths A and B need no
knowledge of servers or code. Every command you need is printed in full: you copy it, paste it,
and press Enter.

Each step has one job. Under most steps you'll find:

- **You'll see:** what should happen, so you know it worked.
- **If it goes wrong:** the most common problem at that step, and the fix.

---

## Start here: which of these are you?

Pick the one that sounds most like you, then follow only that path.

| If this sounds like you… | Follow | Time |
|---|---|---|
| "I want to try PawPoller on my own computer." | [Path A: the desktop app](setup/DESKTOP.md) | 10 minutes |
| "I want it collecting my stats all the time, and I have a computer that stays on. It can be the one I use every day. I'd like to check it from my phone too." | [Path B: always on, on a computer you own](setup/HOME_SERVER.md) | 30 minutes |
| "I don't have a computer that stays on, and I'm happy to rent a small server for about US$6 a month." | [Path C: a rented server](setup/RENTED_SERVER.md) | About an hour |

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
- **Docker**: a tool that runs PawPoller in its own sealed box on a server. Only Path C uses it.

---

## After you install

These are the same whichever path you took:

- **[After you install](setup/AFTER_INSTALL.md)**: finishing setup, connecting your sites, what
  each site needs, updates and backups, and what to do when something goes wrong.
- **[Advanced](setup/ADVANCED.md)** (optional): your own web address instead of Tailscale, the
  credential vault, security settings, running from source, building the app, and how stories
  are stored.
