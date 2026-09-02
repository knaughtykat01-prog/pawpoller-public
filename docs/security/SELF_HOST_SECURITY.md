# Self-hosting PawPoller securely

PawPoller is a single-admin app that holds your platform logins. If you expose its dashboard to the internet, read
this. It complements `SETUP.md` (how to run it) and `ASVS_ASSESSMENT.md` (the formal control assessment).

## Threat model in one line

The dashboard controls everything and the credential vault is the crown jewel: whoever logs in can read your stored
platform credentials (via settings sync / backup). So the whole security story is **"keep login locked down."**

## First-run setup — do it locally

On an **unconfigured** instance (no password set yet) the setup endpoint has to run without auth — there's no password
to check. To stop a remote attacker racing to claim the admin password on an exposed port, **first-run password setup
only works from the machine running PawPoller (localhost)**:

- **Desktop app:** nothing to do — it's local.
- **Server:** open the dashboard from the server itself, or over an SSH tunnel
  (`ssh -L 8420:localhost:8420 you@server` then browse `http://localhost:8420`), and set the password there.
- **Genuinely need remote first-run on a trusted network?** Set `PAWPOLLER_ALLOW_OPEN_SETUP=1` — a conscious opt-in.

Set the password **before** exposing the port. Once a password exists, this restriction lifts (login is now
protected normally).

## Behind a reverse proxy (Caddy/nginx/Traefik)

Terminate TLS at the proxy and forward to PawPoller on loopback. Two settings matter:

- **`PAWPOLLER_FORWARDED_IPS`** must list the proxy's address (default `127.0.0.1`). PawPoller only trusts
  `X-Forwarded-*` headers from these IPs. If your proxy runs on a *different host*, set this to the proxy's IP —
  otherwise PawPoller sees the request as plain `http` and the session cookie is issued **without the `Secure` flag**
  (leakable over any stray http request).
- With the scheme correctly seen as `https`, PawPoller emits **HSTS** (`Strict-Transport-Security`, 1 year) so browsers
  refuse to downgrade to http.

Plain-http LAN use is supported (no Secure/HSTS) but never do it over the public internet.

## Two-factor authentication (TOTP)

Enable it in **Settings → Security**. Requires your account password to turn on (so a hijacked session can't bind an
attacker's authenticator and lock you out).

**Backup codes:** enabling 2FA issues **10 one-time recovery codes** — save them. Each works once in place of an
authenticator code, at login *and* to disable 2FA. If you lose your authenticator, log in with a backup code (or
disable 2FA with a backup code + your password). Regenerate a fresh set any time in Settings → Security. The server
stores only SHA-256 hashes; the plaintext is shown once.

**If you lose both your authenticator and your backup codes:** you can still recover from the server by deleting the
`auth_totp_secret` / `auth_totp_enabled` keys from the encrypted vault (`settings.vault.json`) — this needs the vault
key and shell access, i.e. the local operator. See §5.1 of `SETUP.md` for the vault key.

## Credentials at rest

The **vault is always on.** Platform credentials, the admin password hash, the TOTP secret + backup-code hashes, API
keys and the session-signing secret are Fernet-encrypted in `settings.vault.json` — never in plaintext `settings.json`.

On a **server**, the real question is where the *vault key* lives. By default it sits in a `.vault_key` dotfile next
to the ciphertext, which gives no protection if the disk is read. For real at-rest protection, hold the key
out-of-band and pass it via **`PAWPOLLER_VAULT_KEY`** (or `PAWPOLLER_VAULT_KEY_FILE` pointing at a Docker/K8s secret).
Generate one with:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Brute-force protection

- **Per-IP lockout:** 10 failed attempts (password *or* 2FA) in 5 minutes → that IP is blocked with `429`.
- **Global soft-throttle:** during a distributed (IP-rotating) attack, once failures across all IPs cross a threshold,
  every login attempt is slowed by a couple of seconds. It never hard-locks — your correct login still succeeds.
- **Cloudflare Turnstile** (optional, Settings → Security): the strong control against automated guessing. Recommended
  for any internet-exposed instance.

## Known limitations (be aware)

- **Logout is browser-side only.** Sessions are stateless signed cookies with no server-side revocation list, so
  "log out" clears *your* cookie but a copied/stolen cookie stays valid until it expires (24h, or 30d with
  "remember me"). To revoke *all* sessions immediately, **change your password** (it rotates the signing secret).
- **TOTP codes are replayable within their ~90-second window** (standard TOTP; the rate limiter caps volume).
- **Single admin only.** There are no multi-user roles. Everyone with the password is a full admin.
