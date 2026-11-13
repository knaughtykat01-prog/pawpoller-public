# Privacy Notice — TEMPLATE

> **Fill-in template for self-hosters, not legal advice.** Because PawPoller is self-hosted, **the operator of the
> instance is the data controller**. The PawPoller project receives nothing from this instance unless someone here
> switches on one of the opt-in sends listed under "What it does NOT do". Replace every **[bracketed]** field.

**Instance:** [name], operated by **[you]**. Effective **[date]**.

## What this instance stores

- **Platform credentials** you enter (cookies, API keys, tokens, passwords) — stored **encrypted at rest** in the
  credential vault, used only to post and poll on the connected accounts.
- **Content you manage** — stories, artwork, posts, tags, descriptions, and the analytics polled back from each
  platform (views, favourites, comments, etc.).
- **Supporter figures** from a connected Patreon page: how many patrons, how many in each tier, and the money coming
  in, as totals for each poll. No supporter's name, email or pledge is kept.
- **Operational data** — logs (which may include IP addresses of dashboard logins), the admin password hash, 2FA
  secret, and API-key hashes.

## What it does NOT do

- **Nothing goes to the PawPoller project unless you say yes.** PawPoller makes network calls to (a) the platforms
  you connect, (b) GitHub, to check for updates, and (c) **only if you opted in**, the PawPoller Tech Centre:
  - **technical error reports** — the error and a short log excerpt, with passwords, tokens, cookies, email
    addresses, @handles, other people's names and the paths of web addresses removed before sending;
  - **usage check-ins ("count this copy")**: a random id for this copy, the PawPoller version, whether it runs as the
    desktop app or a server, how it's set up (on its own, or paired with a server), your operating system, the
    processor type, how it was installed, whether the dashboard is open right now, how long it's been running, how
    long the last check-in took to reach us, which kinds of site are connected (never which accounts) and a size range
    for your library. The Tech Centre works out the country your connection comes from (never your address). PawPoller asks
    after it first checks the connected sites successfully, may ask once more on a later day if the question is closed
    without an answer, and the check-ins stay off until someone says yes;
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
favourite or follow, and the text of their comments. Reading a Patreon page goes through its members to count them;
their details are used for the count and never kept. That data belongs to those people too. Use it only to run the
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
