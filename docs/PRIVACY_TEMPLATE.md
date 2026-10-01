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
    are connected (not which accounts) and rough library sizes.

  Both are off until you agree, and either can be switched off in Settings at any time. No analytics service, no
  advertising tracker.
- **No third-party ad/tracking.**

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
