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

- **No telemetry / phone-home.** PawPoller makes network calls only to (a) the platforms you connect and (b) update
  checks you can disable. It sends nothing to the PawPoller project or any analytics service.
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
