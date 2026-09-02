# Instagram setup, step by step

Instagram is the most involved platform PawPoller connects to. Nothing about it is difficult,
but there are roughly six steps that must happen in order, spread across three different places
(the Instagram app, Meta's developer site, and PawPoller), and several of them fail in ways that
do not explain themselves.

This guide is the long form. There is a shorter version inside the app — **Settings → Platforms
→ Instagram**, or the **📖 Setup guide** button on the Instagram tile at `#/platforms`.

**Budget about 20 minutes**, and read §0 before you start — two of those points will cost you
the whole attempt if you find them out later.

---

## Contents

- [0. Read this first](#0-read-this-first)
- [1. Switch your Instagram account to professional](#1-switch-your-instagram-account-to-professional)
- [1b. Register as a Meta developer (if you never have)](#1b-register-as-a-meta-developer-if-you-never-have)
- [2. Create a Meta developer app](#2-create-a-meta-developer-app)
- [3. Add the permissions](#3-add-the-permissions)
- [4. Add yourself as a tester](#4-add-yourself-as-a-tester)
- [5. Generate the long-lived token](#5-generate-the-long-lived-token)
- [6. Connect it in PawPoller](#6-connect-it-in-pawpoller)
- [7. If you want to post, not just track](#7-if-you-want-to-post-not-just-track)
- [8. Keeping the token alive](#8-keeping-the-token-alive)
- [9. Troubleshooting](#9-troubleshooting)
- [10. What PawPoller does and does not do with Instagram](#10-what-pawpoller-does-and-does-not-do-with-instagram)

---

## 0. Read this first

Two things decide whether this works, and both are easier to get right at the start than to
discover at step 5.

### ⚠ Do not use Chrome

**Use Microsoft Edge, or Firefox, or anything that is not Chrome.** Chrome reliably breaks
Meta's developer dashboard and its token generator: the flow either silently does nothing or
completes without ever showing you a token. There is no error message. It simply does not work,
and it looks like you did something wrong.

This is not a theory — it is the single most common way this setup fails.

### ⚠ A personal Instagram account cannot be used at all

The API PawPoller uses only exists for **Business** and **Creator** accounts. If your account is
personal, no amount of correct setup elsewhere will help. Step 1 fixes this and takes about
thirty seconds.

### What you will need

| | |
|---|---|
| An Instagram **Business or Creator** account | Free. Step 1. |
| A **Meta developer account** | Free, registered once with an ordinary Facebook account. §1b — including what to do if its registration loops. |
| A **Meta developer app** | Free. Step 2. You do **not** need a Facebook Page. |
| A **long-lived access token** | Free. Step 5. Lasts ~60 days; PawPoller renews it for you. |

### Where each step happens

Everything is done on a **desktop browser** except step 1, which is done in the Instagram
**mobile app**.

---

## 1. Switch your Instagram account to professional

In the **Instagram mobile app**:

1. Go to your profile → **☰ menu** → **Settings and privacy**.
2. Find **Account type and tools** → **Switch to professional account**.
3. Choose **Business** or **Creator**. Either works.

Instagram will ask you to pick a category and may offer to connect a Facebook Page — **you can
skip the Facebook Page**. PawPoller uses the "Instagram API with Instagram Login" flow, which
does not involve Pages at all.

> **Why:** Meta only exposes media insights (views, reach, saves, shares) for professional
> accounts. On a personal account the endpoints exist but return nothing you can use.

---

## 1b. Register as a Meta developer (if you never have)

Step 2 assumes you already have a Meta developer account. If you do not, you register once, free,
with an ordinary Facebook account — and this is where people get stuck, sometimes badly.

**The symptom:** you complete the registration and, instead of finishing, it sends you back to
"confirm your phone with a code". You enter the code, add your email, pick **Developer** as what
you are, press register — and it returns you to the phone step again. Repeating it changes
nothing.

This is Meta's, not something you are doing wrong, and it happens before PawPoller is involved at
all.

### First: stop retrying

**This is the one piece of advice that is actively useful, and it is counter-intuitive.** Meta
rate-limits phone verification, and every failed attempt makes the next one *less* likely to
work. A loop that might have cleared on its own can harden into "your number is wrong" and then
into no codes arriving at all.

If you have already tried three or four times: **stop, and leave it for several hours.** Not
minutes. Continuing to hammer it is how a recoverable loop becomes a locked one.

### Things to try, once you have waited

1. **Check the number is not already attached to another Facebook account.** Meta will not tell
   you this — it just refuses, and the refusal is indistinguishable from a wrong number. This is
   the most likely cause when the number is definitely correct.
2. **Use WhatsApp verification if it is offered.** It goes through a different path than SMS and
   sometimes works when the SMS route will not.
3. **Check the country code**, especially if the number was entered with a leading zero as well
   as a country code.
4. **Enable two-factor authentication on the Facebook account**, then retry. Meta expects
   developer accounts to have it, and an account without it can be bounced back to phone
   verification.
5. **Some carriers filter Meta's verification shortcode.** If nothing arrives at all and the
   number is right, this is worth ruling out with a different number.
6. **Turn off ad-blockers and strict tracking protection for the site**, or use a clean window.
   The registration carries state between steps and blockers can drop it, which looks exactly
   like being bounced backwards.

### About switching browsers

§0's "do not use Chrome" is a real and documented finding, but it is about the **token
generator** at step 5, and it is worth doing anyway because it costs nothing.

⚠ **It is not a fix for this loop.** In one observed case a tester switched from Chrome to Edge
and the registration still failed, then degraded into phone-verification errors. Do not spend
attempts on browser-swapping in the belief that it is the answer here — the rate limit above is
the more important constraint.

### If it will not budge, park Instagram

**Instagram is the only one of the twenty platforms that requires a developer account at all.**
Everything else — Bluesky, FurAffinity, Weasyl, SoFurry, Mastodon, e621, AO3 and the rest —
connects with an ordinary login, an app password, or an API key, in a couple of minutes each.

There is no dependency between them. Connect everything else first and come back to Instagram
another day, when the rate limit has reset and you are not making decisions while annoyed at a
verification form. Nothing else in PawPoller is waiting on it.

---

## 2. Create a Meta developer app

In a **desktop browser that is not Chrome**, go to **[developers.facebook.com/apps](https://developers.facebook.com/apps)**
and sign in with the Facebook account you want to own this app. It does not need to be connected
to your Instagram account.

1. **Create app**.
2. When asked what you want the app to do, pick the option that offers **Instagram** — the
   wording on this screen changes every few months, so go by the product rather than the exact
   label.
3. Once the app exists, open it and add the **Instagram** product.
4. Inside the Instagram product, choose **API setup with Instagram login**.

That last phrase is the one to steer by. Meta also offers "API setup with Facebook login", which
is a different flow requiring a Facebook Page, and it is not the one PawPoller uses.

> **You do not need App Review.** While your app is in development mode and you are using it with
> your *own* account, Meta requires no review. Review only becomes relevant if you publish the app
> for other people — see §10.

---

## 3. Add the permissions

Still inside **API setup with Instagram login**, add these permissions:

| Permission | Needed for |
|---|---|
| `instagram_business_basic` | Everything. Always add this. |
| `instagram_business_manage_insights` | Stats — views, reach, saves, shares |
| `instagram_business_content_publish` | **Posting** from PawPoller |

**Add the publish permission now even if you only want stats today.** Adding a permission later
means generating a brand-new token and pasting it in again; adding it up front costs nothing.

> **This is the step people get wrong.** A token with only the first two permissions polls your
> stats perfectly and then has **every post rejected**. Reading and writing are separate
> permissions, and a read-only token gives no hint that it is read-only until a post fails.

---

## 4. Add yourself as a tester

Your own Instagram account has to be attached to the app before it will issue a token for it.

1. In the app dashboard, find **Roles** → **Instagram Tester**.
2. Add your Instagram username.
3. Now **accept the invitation from inside Instagram**: in the Instagram app or on the web, go to
   **Settings → Apps and websites → Tester invites** and accept.

The invitation is easy to miss — Instagram does not notify you about it in any prominent way. If
step 5 refuses to produce a token for your account, this unaccepted invite is the first thing to
check.

---

## 5. Generate the long-lived token

Back in **API setup with Instagram login**, find the token generator for your account.

1. Click to generate an access token for your Instagram account.
2. Instagram will ask you to approve the permissions you added in step 3. Approve them.
3. Copy the token. It is long — several hundred characters. Copy all of it.

**Copy it somewhere safe before leaving the page.** Meta does not always let you view an existing
token again; if you lose it you generate a new one, which is not a disaster, but it is avoidable.

> If nothing happens when you click generate, or the token box stays empty: **you are almost
> certainly in Chrome.** See §0.

---

## 6. Connect it in PawPoller

### Desktop or web dashboard

1. **Settings → Platforms → Instagram**.
2. Paste the token into **Access token**.
3. **User ID** is optional — leave it blank and PawPoller uses `me`, which resolves to whichever
   account the token belongs to. Fill it in only if you have a specific reason to pin it.
4. Press **Connect**.

On success the accordion's dot turns green and shows the username the token actually belongs to.
That username is worth reading rather than skimming: it is confirmation that the token is for the
account you think it is.

### Headless / Docker

Set these in `.env` and restart:

```bash
IG_ACCESS_TOKEN=your_long_lived_token_here
# Optional — defaults to "me"
# IG_USER_ID=
```

You can also paste the token through the web dashboard exactly as above; the `.env` route just
saves a step on first deploy.

### Check it worked

Press **IG Poll Now** in the same panel. Your recent posts should appear at `#/ig` within a few
seconds.

---

## 7. If you want to post, not just track

Tracking works the moment step 6 succeeds. **Posting needs one more thing**, and it is the part
of Instagram that surprises people.

### Why there is an extra step

**Instagram never accepts image bytes.** Meta's Content Publishing API takes a public `image_url`
and fetches the picture from that address itself. So PawPoller has to put your image somewhere
Meta can reach before it can post it.

This also means **every Instagram post requires a photo**. There is no text-only Instagram post,
and PawPoller will refuse the attempt rather than fail halfway.

### If PawPoller runs on a server

Set the public base URL of your instance:

```bash
IG_PUBLIC_BASE_URL=https://pawpoller.example.com
```

PawPoller then hosts the image on itself: it converts and downscales the picture to a web-safe
JPEG, serves it at an unguessable one-off URL with a **15-minute lifetime**, hands Meta the URL,
publishes, and deletes the stash afterwards.

The address must be genuinely reachable from the internet — Meta is the one fetching it. A
`localhost` address or a private LAN IP will not work.

### If PawPoller runs on your desktop

The desktop app binds to `localhost`, which Meta cannot reach, so it borrows your server instead.

**Pair the desktop with your server** in **Settings → Posting** — the same pairing used for story
and artwork sync. Nothing else to configure. When you post, the desktop uploads the image to your
server, gets a public URL back, and uses that. The server cleans up after itself.

If you have neither a public URL nor a pairing, PawPoller says so clearly before posting rather
than failing partway through.

### What gets posted

- **Caption** — the artwork's description, or its title if there is no description.
- **Hashtags** — your tag set, appended to the caption. Sanitised to letters, digits and
  underscores, deduplicated, and **capped at Instagram's limit of 30**.

---

## 8. Keeping the token alive

Long-lived tokens last about **60 days**, and **PawPoller refreshes yours automatically** while it
polls. In normal use you should never have to think about this.

If it does lapse — the app was off for two months, or the token was revoked — generate a fresh one
(step 5) and paste it in again (step 6). Nothing else needs redoing: your app, permissions and
tester role all stay valid.

---

## 9. Troubleshooting

### "Session expired — re-enter credentials", but the token is new

**Check your Meta app's status before touching the token.** Meta returns an app-level block as
`OAuthException code 200, "API access blocked"`, which is a problem with the *app*, not the
credential. PawPoller distinguishes these:

| What you see | What it means |
|---|---|
| 🔴 **Red — "expired, re-enter credentials"** | Meta code 190. The token genuinely is expired or invalid. Generate a new one. |
| 🟡 **Amber — "couldn't verify"** | An app-level block, a permissions problem, a rate limit, or a network blip. **The token is probably fine.** Check the app's status in the Meta dashboard. |

Unblocking the app is a Meta-side action; PawPoller can only report it honestly.

> **If Threads breaks at the same moment, that is a strong clue.** Threads and Instagram are both
> Meta and are usually configured under the *same* Meta app, so an app-level block takes out both
> simultaneously. Two platforms failing in the same minute points at the app, not at two
> coincidentally expired tokens.

### Posts appear but views, reach, saves and shares are all zero

Meta rejects an entire insights request if any single requested metric is invalid for that media
type. PawPoller degrades gracefully — likes and comments come straight off the media object and
are always captured — but the insight metrics land as zeros for that post.

Also note **`impressions` was deprecated** for media created after **2 July 2024**. PawPoller
tracks **`views`**, its replacement, so older and newer posts are not directly comparable on that
metric.

### The token generator does nothing

Chrome. See §0.

### I cannot even register as a developer — it keeps asking for a phone code

A registration loop on Meta's side, before any of this applies. See §1b.

### It will not issue a token for my account

The tester invitation is unaccepted. See §4 — accept it inside Instagram under
**Settings → Apps and websites → Tester invites**.

### Everything is connected but posting is rejected

Your token was generated before you added `instagram_business_content_publish`. Permissions are
baked into the token at generation time, so adding the permission is not enough — **generate a new
token** (step 5) and paste it in again.

---

## 10. What PawPoller does and does not do with Instagram

**Tracks:** views, reach, likes, comments, saved and shares per post, over time — with the same
dashboard, growth charts, comparisons, CSV export, trend detection and notifications as every
other platform.

**Posts:** yes, both from the Posts module and as a first-class artwork target, so Instagram can
be ticked alongside Inkbunny, FurAffinity, Weasyl and the rest.

**Edits:** no. **Instagram has no photo edit or replace API**, so PawPoller treats it as
post-only. When you push a correction to a piece from a Masterpiece, Instagram is skipped
deliberately and labelled as such, rather than being quietly passed over.

**Insight calls are one-per-post.** Instagram offers no batch insights endpoint, so a large
back-catalogue polls more slowly than platforms that return everything in one call. This is
Instagram's shape, not a PawPoller limitation.

### One honest caveat

Meta gates this API behind app review for any app used by people other than its owner, and its
policies remove adult content. For your own account in development mode none of that applies. But
if you were hoping to hand this setup to other people through a published app, expect review, and
expect adult content to be a problem with Meta specifically.

---

## Related

- [`SETUP.md` §5](SETUP.md#5-platform-credentials) — credentials for all twenty platforms, and
  what each needs to poll versus to post
- [`SELF_HOSTING.md`](SELF_HOSTING.md) — running PawPoller on a server, which is also what gives
  you the public URL Instagram posting needs
