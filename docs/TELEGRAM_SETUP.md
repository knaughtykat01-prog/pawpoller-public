# Telegram setup, step by step

Telegram is the easiest posting target PawPoller supports, and one of the most useful: a channel
is a direct line to people who chose to follow you, with no algorithm deciding who sees it.

Setup takes about five minutes. There is a shorter version inside the app — the **📖 Setup guide**
button on the Telegram tile — and this is the long form, including the two things that trip people
up.

---

## Contents

- [0. The two things that catch people](#0-the-two-things-that-catch-people)
- [1. Create a bot](#1-create-a-bot)
- [2. Create your channel](#2-create-your-channel)
- [3. Add the bot as an admin](#3-add-the-bot-as-an-admin)
- [4. Connect it in PawPoller](#4-connect-it-in-pawpoller)
- [5. What you can post](#5-what-you-can-post)
- [6. Options, and where to set them](#6-options-and-where-to-set-them)
- [7. Troubleshooting](#7-troubleshooting)
- [8. What Telegram can't do](#8-what-telegram-cant-do)

---

## 0. The two things that catch people

### ⚠ "Post Messages" is a separate permission

Making the bot an **administrator** is not enough. Channel admin rights are a list of individual
toggles, and **Post Messages** is its own line. A bot can be a correctly-added admin and still have
every post rejected.

This is the single most common failure, and it looks like a broken integration rather than a
missing tickbox.

### ⚠ A private channel has no username

A private channel has a **title** — the name you see at the top. That is not a handle. Private
channels have no `@username` at all, and can only be addressed by a numeric **`-100…` id** that
Telegram's interface never displays anywhere.

Typing the title instead is worse than simply failing: a bare word is treated as a public
`@username`, and **someone else may already own it**. PawPoller will then reach a stranger's channel,
confirm it as valid, and fail only when it tries to post.

**You do not need to hunt for the id.** PawPoller's **🔍 Find my channel** button fetches it. See §4.

---

## 1. Create a bot

Message **[@BotFather](https://t.me/BotFather)** on Telegram and send:

```
/newbot
```

He asks for a name (anything) and a username (must end in `bot`). He then replies with a **token**:

```
123456789:AAHk-ExampleTokenExampleTokenExample
```

That entire string is the token. **It is the bot** — anyone holding it can post as it — so treat it
like a password. If it ever leaks, `/revoke` in BotFather issues a new one.

> **Two bots, always.** The bot you make here is for the channel. If you also use PawPoller's
> Telegram notifications (Settings → Telegram, the private alerts and digests), that is a
> *different* bot — PawPoller will not let one token do both jobs. The reason is not tidiness: a
> bot that is an admin of a channel knows the channel's chat, and a notification setup that
> borrowed it once sent a full analytics digest into a public channel. Two bots means your private
> numbers and your public posts can never be confused, and it lets each bot read its own channel's
> reactions.

---

## 2. Create your channel

In Telegram: **New Channel**. Give it a name and a description.

**Public or private both work.**

| | Public channel | Private channel |
|---|---|---|
| Has a `@username` | ✅ | ❌ — none at all |
| What you give PawPoller | `@yourchannel` | its numeric `-100…` id |
| Links back to posts | `t.me/yourchannel/123` | none — Telegram has no public URL for a private post |

Private is a good choice while you are testing: nothing you send is visible to the world. You can
make it public later.

---

## 3. Add the bot as an admin

In the channel: **Manage → Administrators → Add Admin →** your bot.

Then **tick "Post Messages"**. See §0 — this is the step that gets missed.

If you plan to use the **Pin** option, tick **Pin Messages** too. That is also separate.

---

## 4. Connect it in PawPoller

1. **Post any message in the channel** first — "hello" is fine. An admin bot receives channel posts,
   and that is how PawPoller learns the channel's id.
2. **Settings → Platforms → Telegram**.
3. Paste the **bot token**.
4. Press **🔍 Find my channel**. PawPoller asks Telegram which channels your bot can see and fills
   the field in. If it finds several, it lists them by title so you can pick.
5. Press **Save & send test**.

The test sends a real message and reports **which channel it reached** — *"Test message posted to
"My Channel""*. Read that name. It is the difference between "the setup works" and "the setup works
and it is pointed at the right place".

### Headless / Docker

The same fields are in the web dashboard. There is no `.env` shortcut for Telegram channel posting
— the discovery step needs the bot to be live and an admin first.

---

## 5. What you can post

Telegram is a **posting target only** — PawPoller never reads stats from it, because a channel has
none to read beyond view counts on individual posts.

**Artwork.** Tick Telegram when publishing a piece and the image goes straight to the channel with
its description and tags. Telegram takes the file directly, so unlike Instagram there is no public
image host to configure — nothing to set up at all.

**Stories.** A story is **announced**, not posted. Telegram caps a message at 4,096 characters —
about 700 words, against stories that run to tens of thousands — so the announcement carries the
cover image, a blurb, and **links to where the story is actually published**. A channel is an
announcement feed, not an archive.

**Anything you compose.** The Posts page (`#/posts`) sends text or images to the channel alongside
Bluesky, Mastodon, Threads, Tumblr, X and Instagram.

---

## 6. Options, and where to set them

Seven options, each settable **channel-wide** and overridable **on any single piece**.

| Option | What it does |
|---|---|
| **Blur** | Tap-to-reveal spoiler. Follows the piece's rating unless you set it. |
| **Hashtags** | Append your tags as hashtags. |
| **Caption** | Send any text at all, or just the image. |
| **Stop forwarding & saving** | Telegram's own anti-repost control — viewers cannot forward the post or save the image. |
| **Full quality** | Send the original file instead of a compressed photo. |
| **Silent** | Deliver with no notification ping. |
| **Pin** | Pin the message after posting. |

**Channel-wide:** Settings → Telegram → *Channel defaults for published work*.

**Per piece:** an artwork's edit form → **📣 Telegram options**. A story's metadata drawer → **📣
Telegram announcement**.

Each per-piece control has three states: **Default** (follow the channel setting), **On**, **Off**.
Leaving one on Default means it keeps following the channel — so if you change your mind
channel-wide later, that piece follows along.

### The two worth understanding

**Stop forwarding & saving** is the closest thing any platform PawPoller posts to offers as an
anti-repost control. It is off by default, because it also blocks people sharing your work
legitimately. That trade is yours to make, not ours.

**Full quality** matters more than it sounds. Telegram **re-encodes every photo it receives** —
fine for a snapshot, lossy for artwork. This option sends the original file untouched. The cost is
that it appears as an attachment rather than an inline image, which is why it is not the default.

---

## 7. Troubleshooting

### "Telegram accepted the channel but refused the post"

Read the rest of the message — PawPoller reports Telegram's own reason and the channel it reached.

- **"not enough rights to send text messages"** → the bot is an admin without **Post Messages**
  (§3).
- **"bot is not a member of the channel chat"** → you have reached a *different* channel from the
  one you meant. Almost always a bare name that resolved to someone else's public `@username`. Use
  **🔍 Find my channel**.

### The test says it worked, but nothing appeared in my channel

Check the channel name in the success message. If it is not yours, see above — the post went
somewhere real, just not where you meant.

### Find my channel says it can't see anything

Post a message in the channel first, then try again. The bot only learns about a channel when
something happens in it while the bot is an admin.

### "Channel posting needs its own bot token"

You were posting with the notification bot, which 4.8.0 no longer allows. Make a second bot in
BotFather, add it to the channel as an admin with Post Messages, paste its token under Channel
posting. Your channel and its settings are unchanged.

### "Your notification chat is a channel or group"

The red row under Settings → Telegram. Your alerts were pointed at a channel, so PawPoller has
stopped sending them. Press Disconnect, then send `/start` to the notification bot **from your
own account** and press Connect again — it now only accepts your private chat.

### The pin didn't happen but the post did

**Pin Messages** is a separate admin right from **Post Messages**. PawPoller deliberately treats a
failed pin as a warning, not a failed post — the post is live and correct.

---

## 8. What Telegram can't do

**It cannot be edited.** Telegram's API refuses to edit any message older than **48 hours**, so
PawPoller treats Telegram as post-only and skips it when you sync a correction to your other sites.
It is listed as skipped rather than quietly passed over. A correction means deleting the post and
sending a new one, which changes its link.

**It has no stats to poll.** PawPoller tracks nothing from Telegram, so it never appears in your
analytics. That is a property of channels, not a gap in the app.

**A private channel's posts have no public link.** There is no URL to record, so PawPoller stores
the post's id and no link.

---

## Related

- [`SETUP.md` §5](SETUP.md#5-platform-credentials) — credentials for every platform, and what each
  needs to poll versus to post
- [`INSTAGRAM_SETUP.md`](INSTAGRAM_SETUP.md) — the other image target, and a useful contrast:
  Instagram needs a public image host, a Meta developer account and a Business account, where
  Telegram needs a bot and a tickbox
