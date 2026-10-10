# Privacy Notice — TEMPLATE

> **Fill-in template for self-hosters, not legal advice.** Because PawPoller is self-hosted, **the operator of the
> instance is the data controller** — the PawPoller project receives nothing. Replace every **[bracketed]** field.

**Instance:** [name], operated by **[you]**. Effective **[date]**.

## What this instance stores

- **Platform credentials** you enter (cookies, API keys, tokens, passwords) — stored **encrypted at rest** in the
  credential vault, used only to post and poll on the connected accounts.
- **Content you manage** — stories, artwork, posts, tags, descriptions, and the analytics polled back from each
  platform (views, favourites, comments, etc.).
- **Operational data** — logs (which may include IP addresses of dashboard logins), the admin password hash, 2FA
  secret, and API-key hashes.

## What it does NOT do

- **Nothing goes to the PawPoller project unless you say yes.** PawPoller makes network calls to (a) the platforms
  you connect, (b) GitHub, to check for updates, and (c) **only if you opted in**, the PawPoller Tech Centre:
  - **technical error reports** — the error and a short log excerpt, with passwords, tokens, cookies, email
    addresses, @handles, other people's names and the paths of web addresses removed before sending;
  - **anonymous usage check-ins** — a random install ID, the app version, operating system, which platform types
    are connected (not which accounts) and rough library sizes;
  - **the Instagram picture relay** — the picture being posted to Instagram, held for 15 minutes so Instagram can
    fetch it, plus the uploader's IP address for 10 minutes (rate limits).

  All three are off until someone agrees; PawPoller records when and to which wording, and any of them can be
  switched off in Settings at any time. The PawPoller project's own policy covers what it does with them:
  https://pawpoller.com/privacy. No analytics service, no advertising tracker.
- **No third-party ad/tracking.**

## Age

PawPoller asks at setup whether the person is 18 or older and stores the answer on this instance. For anyone who says
they're under 18, adult ratings, adults-only sites and switching safe mode off are locked. [Say here whether this
instance accepts under-18 users at all.]

## Other people's data

Polling brings in other people's information from the connected sites: the names and handles of people who comment,
favourite or follow, and the text of their comments. That data belongs to those people too. Use it only to run the
instance, don't publish or share it elsewhere, and delete it on request where you can. [Name how someone can ask.]

## Where the data lives

On the operator's own machine or server (`%APPDATA%\PawPoller\` on Windows, `~/.local/share/PawPoller/` on Linux, or a
Docker volume). It is not shared with anyone except the destination platforms when you publish.

## Retention & deletion

Data persists until the operator deletes it. To remove everything, uninstall and delete the data directory (or the
Docker volume). Individual works/posts/credentials can be deleted in-app.

## Third parties

Publishing sends your content to the destination platforms **you choose**, each governed by its own privacy policy.
Optional integrations (Telegram notifications, Cloudflare Turnstile, a Discord announce webhook) send data only if you
configure them.

## Your requests

Contact the operator at **[contact]** for access to, correction of, or deletion of your data on this instance.
