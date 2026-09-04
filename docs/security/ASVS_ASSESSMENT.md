# PawPoller — OWASP ASVS 5.0 Level 2 Self-Assessment

**Standard:** [OWASP Application Security Verification Standard 5.0.0](https://github.com/OWASP/ASVS/releases/tag/v5.0.0_release)
**Target level:** L2 (appropriate for an application that stores sensitive credentials)
**Scope:** All 253 Level 1 + Level 2 requirements across the 17 chapters.
**Assessed against:** PawPoller `2.102.0`.
**Date:** 2026-07-13.
**Amended:** 2026-08-21 (3.17.3) — V3.4.3 `form-action`; see that entry. The rest of the assessment has *not* been re-run against 3.17.3.

## What this is (and is not)

This is a **self-assessment**, not a certification. It was produced by walking every
L1/L2 requirement of ASVS 5.0 against the PawPoller source, with file/line evidence, and
fixing the cheap gaps in the same pass. It is published for **accountability and
transparency** — so a prospective self-hoster can see exactly how the app treats their
credentials, and so future changes have a baseline to regress against. It has **not** been
reviewed by an independent third party.

Verdicts are honest, including the gaps. Where a requirement is **N/A**, the reason is
stated rather than silently skipped.

## Threat model (this is load-bearing for the verdicts)

PawPoller is a **single-tenant, self-hosted** application. Each deployment is run by, and
for, **one operator** who owns the machine. There are no multiple users, no tenant
separation, no untrusted "consumers" of an API. The only authentication surface is the
dashboard login, which gates one all-or-nothing role. The "sensitive data" the app protects
is **the operator's own platform credentials** (cookies/tokens/passwords for up to 16
publishing/analytics platforms).

This shapes applicability throughout:

- **Authorization (V8), multi-tenancy (V8.4), IDOR/BOLA** — largely N/A: there is one user
  and one data owner. There is no cross-user data to leak.
- **OAuth/OIDC as an authorization server (V10)** — N/A: PawPoller is an OAuth *client* to
  external platforms, never an authorization server, and dashboard login is not OIDC.
- **WebRTC (V17), GraphQL (V4.3), WebSockets (V4.4)** — N/A: none are used.
- **TLS (V12), HSTS, HTTP→HTTPS redirect** — the app binds to loopback and expects a
  TLS-terminating reverse proxy (e.g. Caddy or nginx with Let's Encrypt). These controls live
  at the **edge**, which is the self-hoster's responsibility; verdicts note this as "edge".
  (Since **2.185.0** the app also emits its own `Strict-Transport-Security` header when it sees
  an https request, so HSTS is belt-and-braces.)
- The most consequential residual risk is **credential custody** — see the Known Gaps
  register.

> **2.185.0 hardening pass** (see `SELF_HOST_SECURITY.md` for the operator-facing version). Closed: an
> unauthenticated first-run-setup takeover on an exposed unconfigured instance (setup is now loopback-gated, opt-out
> `PAWPOLLER_ALLOW_OPEN_SETUP=1`). Added: **2FA backup/recovery codes** (recovery path for a lost authenticator;
> require-password to enable), a **global soft-throttle** on distributed brute-force, **HSTS**, and constant-time
> API-key + username-timing paths. Still-open known gaps below (stateless logout non-revocation, breached-password
> check, SSRF thumbnail proxy) are unchanged and now also captured in the self-host doc's "Known limitations".

## Scoring legend

| Mark | Meaning |
|------|---------|
| **PASS** | Requirement met, with evidence. |
| **PARTIAL** | Substantially met, with a documented shortfall or an unverified element. |
| **GAP** | Not met. Listed in the Known Gaps register with rationale and any mitigation. |
| **N/A** | The relevant feature/technology is not present in PawPoller; reason stated. |
| **EDGE** | Satisfied at the reverse-proxy/hosting layer, not in application code (self-hoster's responsibility; met in the reference deployment). |

## Scorecard by chapter (L1 + L2)

| Chapter | Reqs | PASS | PARTIAL | GAP | N/A / EDGE |
|---|---:|---:|---:|---:|---:|
| V1 Encoding & Sanitization | 27 | 12 | 2 | 0 | 13 |
| V2 Validation & Business Logic | 11 | 6 | 4 | 0 | 1 |
| V3 Web Frontend Security | 19 | 12 | 3 (2 EDGE) | 1 | 3 |
| V4 API & Web Service | 10 | 3 | 0 | 0 | 7 (1 EDGE) |
| V5 File Handling | 9 | 3 | 4 | 2 | 0 |
| V6 Authentication | 35 | 15 | 3 | 5 | 12 |
| V7 Session Management | 18 | 6 | 5 | 2 | 5 |
| V8 Authorization | 7 | 3 | 0 | 0 | 4 |
| V9 Self-contained Tokens | 7 | 5 | 0 | 0 | 2 |
| V10 OAuth & OIDC | 29 | 1 | 1 | 0 | 27 |
| V11 Cryptography | 14 | 11 | 3 | 0 | 0 |
| V12 Secure Communication | 9 | 3 | 0 | 0 | 6 (5 EDGE) |
| V13 Configuration | 13 | 9 | 2 | 1 | 1 |
| V14 Data Protection | 9 | 5 | 3 | 1 | 0 |
| V15 Secure Coding & Architecture | 13 | 6 | 6 | 0 | 1 |
| V16 Logging & Error Handling | 16 | 11 | 3 | 1 | 1 |
| V17 WebRTC | 7 | 0 | 0 | 0 | 7 |

**Headline:** Of the requirements that **apply** to a single-tenant self-hosted app (i.e.
excluding N/A/EDGE), the large majority PASS. The residual GAPs are concentrated in
areas that are either intrinsic to the stateless-session / single-operator design or would
require third-party services (breached-password APIs, antivirus, remote log shipping) that
don't fit a self-hosted tool. None is a remote-code-execution, injection, or
credential-disclosure class defect.

## Fixed in this assessment pass (2.102.0)

The walk-through surfaced and **closed** the following. Each has a regression test.

1. **V1.2.2 (L1) — `javascript:` URL scheme could reach an `href`.** Scraped submission
   permalinks (`sub.link`, `d.url`, `external_url`) were HTML-escaped but not
   scheme-validated, so a malicious `javascript:` URL from scraped platform data would
   execute on click. Added `Utils.safeUrl()` (allowlists http(s)/relative/blob/`data:image`)
   and wrapped all external-URL `href` sinks (`app.js` ×16, plus `artwork.js`,
   `submissions.js`, `collections.js`, `bookshelf.js`).
2. **V1.2.1 / attribute-injection — unescaped thumbnail in a CSS context.**
   `submissions.js` interpolated `w.thumb_url` raw into
   `style="background-image:url('…')"`. Added `Utils.cssUrl()` (scheme-checks, then
   percent-encodes the CSS/HTML breakout set — note `encodeURIComponent` leaves `'()` alone,
   so those are encoded explicitly).
3. **V3.4.3 (L2) — CSP missing `object-src` and `base-uri`.** `base-uri` has no `default-src`
   fallback, so a `<base>` injection was unconstrained. Both directives added to the main
   and epub-viewer CSPs (`object-src 'none'; base-uri 'none'`).
4. **V13.4.5 (L2) — interactive API docs exposed.** FastAPI's `/docs`, `/redoc`,
   `/openapi.json` were served by default. Now disabled unless `PAWPOLLER_ENABLE_DOCS=1`.
5. **V16.3.1 / V16.2.1 (L2) — authentication events were not logged.** Login success/failure,
   bad-2FA, bot-check failure, rate-limit trips, and rejected API keys are now logged **with
   client IP and (sanitized) username**. Middleware now also counts a rejected API key toward
   the rate limiter.
6. **V16.4.1 (L2) — log injection.** The attacker-controlled username is passed through
   `_sanitize_for_log()` (strips CR/LF/control chars, caps length) before it reaches a log
   line.
7. **V16.5.1 (L2) — internal error detail leaked to clients.** ~200 routes raise
   `HTTPException(500, detail=str(e))`, returning raw exception text (paths, network errors).
   A `StarletteHTTPException` handler now scrubs any **5xx** detail to `"Internal server
   error"` (logging the real detail server-side) while leaving **4xx** validation messages
   intact.
8. **V7.4.3 (L2) — password change didn't end other sessions.** Because sessions are
   stateless signed cookies, a stolen or concurrent session survived a password change.
   `config.rotate_session_secret()` now runs on password change, invalidating **all** issued
   cookies (including the caller's — they re-login).
9. **Log hygiene (V16) — unbounded log files.** Plain `FileHandler` → `RotatingFileHandler`
   (10 MB × 5) in both `server.py` and `main.py`.

Tests: `tests/test_error_scrub.py`, `tests/test_session_rotation.py`, plus the pre-existing
`test_auth_gate.py`, `test_vault_always_on.py`, `test_vault_key.py`.

---

## Per-chapter adjudication

Notation: each line is `REQ (level) — VERDICT — evidence/rationale`. N/A-by-technology lines
are grouped where a whole family doesn't apply.

### V1 — Encoding and Sanitization

- **V1.1.1, V1.1.2 (L2) — PASS.** Output encoding is applied at the interpolation boundary
  (`Utils.escapeHtml`, `utils.js:154`) and JSON responses are encoded by FastAPI, not by hand.
- **V1.2.1 (L1) — PASS.** Context-correct encoding: `escapeHtml` for HTML text/attributes;
  `Utils.cssUrl` for the one CSS `url()` context (fixed this pass).
- **V1.2.2 (L1) — PASS (fixed this pass).** `Utils.safeUrl` disallows `javascript:`/`data:text`
  before a URL reaches an `href`.
- **V1.2.3 (L1) — PASS.** No hand-built JS; JSON is generated by `json`/FastAPI. No `eval`.
- **V1.2.4 (L1) — PASS.** All SQL values are `?`-parameterized; table/column identifiers come
  from hardcoded maps (`PLATFORM_TABLES`) or are validated against `config.ALLOWED_GOAL_METRICS`.
  (Independently checked by the security review in 2.100.0.)
- **V1.2.5 (L1) — PASS.** No `shell=True` on user input; subprocess calls use argument lists;
  generated uninstall/update shell scripts `shlex.quote` every interpolated path (2.100.0).
- **V1.2.6 LDAP, V1.2.7 XPath, V1.2.8 LaTeX, V1.3.8 JNDI, V1.3.9 memcache (L2) — N/A.** None
  of these interpreters/backends are used.
- **V1.2.9 (L2) — N/A.** No path takes user input into a compiled regex as metacharacters.
- **V1.3.1 (L1), V1.3.5 (L2) — N/A (single-author).** The Markdown/BBCode/HTML the app
  processes is the **operator's own** story content, converted for upload to external
  platforms — not third-party input rendered as a security boundary in the dashboard. There
  is no WYSIWYG HTML ingestion from an untrusted party.
- **V1.3.2 (L1) — PASS.** No `eval()`/`new Function()` anywhere in app code (only inside a
  vendored library).
- **V1.3.3 (L2) — PARTIAL.** Untrusted text is escaped before dangerous contexts; length
  trimming is enforced in some places (e.g. titles) but not uniformly documented per field.
- **V1.3.4 SVG (L2) — N/A.** No user-supplied SVG is rendered.
- **V1.3.6 (L2) — PARTIAL (SSRF).** See Known Gaps §KG-1: the thumbnail proxy endpoints fetch
  a caller/scrape-supplied URL. Mitigated by auth-gating and single-tenancy; no scheme/host
  allowlist yet.
- **V1.3.7 template injection (L2) — N/A.** No server-side templating of untrusted input
  (f-strings interpolate already-escaped values; no Jinja user-templates).
- **V1.3.10 format strings (L2) — PASS.** Logging uses `%`-style lazy args; no user-controlled
  format strings.
- **V1.3.11 SMTP/IMAP injection (L2) — PARTIAL.** The only mail path is an operator-triggered
  password-reset to the operator's **own** fixed address; no user-controlled header fields.
- **V1.4.1–V1.4.3 memory safety (L2) — N/A.** Python is memory-safe; no manual pointer/buffer
  handling.
- **V1.5.1 (L1) — PASS.** XML is parsed only via Python's stdlib `xml.etree`, which does not
  resolve external entities by default (no XXE); no `lxml` with `resolve_entities` on
  untrusted input.
- **V1.5.2 (L2) — PASS.** No `pickle`/unsafe deserialization of untrusted data; inputs are JSON.

### V2 — Validation and Business Logic

- **V2.1.1 (L1) — PASS.** Input validation rules are documented (this file + inline): allowlists
  for poll intervals `{15,30,60,120,240,360,480,600,720}`, goal metrics, and platform codes.
- **V2.1.2, V2.1.3 (L2) — PARTIAL.** Combined-item and business-limit rules are documented here
  at a high level, not exhaustively per endpoint.
- **V2.2.1 (L1) — PASS.** Security-relevant input is positively validated against allowlists.
- **V2.2.2 (L1) — PASS.** Validation is enforced server-side (`routes/api.py save_preferences`
  rejects out-of-set values regardless of the client).
- **V2.2.3 (L2) — PARTIAL.** Reasonableness of combined items is checked where it matters
  (account↔platform pairing) but not universally.
- **V2.3.1 (L1) — N/A.** Not a sequential-workflow app; there is no multi-step business flow to
  order-enforce.
- **V2.3.2 (L2) — PARTIAL.** Documented limits exist for polling cadence; other limits are
  implicit.
- **V2.3.3 (L2) — PASS.** DB writes use SQLite transactions; the "commit before any `await`"
  rule (CLAUDE.md) prevents partial-write states in pollers.
- **V2.3.4 locking limited resources (L2) — N/A.** No limited-quantity resource (seats/slots).
- **V2.4.1 (L2) — PARTIAL.** Anti-automation on the login (10 failures / 5 min / IP) and
  per-platform polling delays; there is no general per-endpoint API rate limit (single-tenant,
  authenticated-operator-only surface).

### V3 — Web Frontend Security

- **V3.2.1 (L1) — PASS.** `X-Content-Type-Options: nosniff` on all responses; downloads use
  appropriate content types.
- **V3.2.2 (L1) — PASS.** Text is rendered via `textContent` (442 sites) or `escapeHtml`; no
  raw untrusted text into `innerHTML`.
- **V3.3.1 (L1) — PARTIAL.** The `pp_session` cookie is `Secure` when the scheme is HTTPS,
  `HttpOnly`, `SameSite=Lax`. It does **not** use the `__Host-`/`__Secure-` name prefix, and
  `Secure` is necessarily omitted on desktop `http://localhost`. See KG-2.
- **V3.3.2 (L2) — PASS.** `SameSite=Lax` chosen deliberately (a prod incident ruled out
  `Strict`; documented in code).
- **V3.3.3 (L2) — GAP.** No `__Host-` cookie prefix. See KG-2.
- **V3.3.4 (L2) — PASS.** Session cookie is `HttpOnly` and only set via `Set-Cookie`.
- **V3.4.1 (L1) — EDGE.** HSTS is set by the reverse proxy (Caddy) in the reference
  deployment; not emitted by the app (which serves loopback HTTP behind the proxy).
- **V3.4.2 (L1) — PASS.** CORS `allow_origins=[]` (no cross-origin requests permitted);
  `allow_credentials=False`.
- **V3.4.3 (L2) — PASS (fixed this pass; strengthened in 3.17.3).** CSP includes
  `object-src 'none'`, `base-uri 'none'` and — since 3.17.3 — `form-action 'self'`, plus an
  allowlist (`default-src 'self'`, hashed inline theme script).
  ⚠ **`form-action` was the gap this assessment missed.** It shares the no-`default-src`-fallback
  property that justified naming `object-src`/`base-uri` here, but was not named, so an injected
  `<form action="https://attacker/">` could post from our own origin with **no script involved** —
  which `script-src 'self'` does nothing about. A pre-release review found the matching sink (an
  unescaped query parameter in the DA OAuth callback, `routes/da_api.py`); both halves are fixed.
  Applied to **all three** CSP builders, including `_build_share_csp`, which serves public pages.
  Lesson for the next refresh: enumerate the directives that do **not** inherit from `default-src`
  and check each, rather than checking the two this requirement happens to name.
- **V3.4.4 (L2) — PASS.** `X-Content-Type-Options: nosniff`.
- **V3.4.5 (L2) — PASS.** `Referrer-Policy: strict-origin-when-cross-origin`.
- **V3.4.6 (L2) — PASS.** CSP `frame-ancestors 'none'` on every response.
- **V3.5.1, V3.5.2 (L1) — PASS.** State-changing requests use JSON bodies
  (`Content-Type: application/json` is not a CORS-safelisted value → cross-origin requests
  trigger a preflight, which the empty CORS allowlist rejects). Combined with `SameSite=Lax`,
  this closes the CSRF surface without a separate token.
- **V3.5.3 (L1) — PASS.** Sensitive actions use POST/PATCH/DELETE; the only state-reading GET
  that returns bulk data (`/api/backup/database`) is auth-gated and non-mutating.
- **V3.5.4 (L2) — N/A.** Single application, single hostname.
- **V3.5.5 (L2) — N/A.** No `postMessage` usage in app code.
- **V3.7.1 (L2) — PASS.** Vanilla JS only; no Flash/Silverlight/applets.
- **V3.7.2 (L2) — PASS.** No auto-redirect to a user-supplied hostname (no open redirect).

### V4 — API and Web Service

- **V4.1.1 (L1) — PASS.** FastAPI responses carry `Content-Type` with charset (JSON = UTF-8).
- **V4.1.2 (L2) — EDGE.** HTTP→HTTPS redirect is handled by the proxy for the browser-facing
  origin; the app itself does not transparently redirect API callers.
- **V4.1.3 (L2) — PASS.** `X-Forwarded-*` is trusted only from the configured proxy
  (`uvicorn --forwarded-allow-ips = DASHBOARD_FORWARDED_IPS`), so an end user cannot spoof the
  client IP used for rate limiting / Secure-cookie decisions.
- **V4.2.1 request smuggling (L2) — EDGE.** HTTP framing handled by uvicorn + Caddy; single
  origin.
- **V4.3.1, V4.3.2 GraphQL (L2) — N/A.** No GraphQL.
- **V4.4.1–V4.4.4 WebSocket (L1/L2) — N/A.** No WebSockets.

### V5 — File Handling

- **V5.1.1 (L2) — PARTIAL.** Permitted types/sizes are documented here and in code (e.g. IG
  upload capped at 12 MB; image extension allowlists on import) but not for every upload path.
- **V5.2.1 (L1) — PARTIAL.** The IG upload enforces a 12 MB cap; some other import paths rely
  on the source platform's own limits rather than an explicit local cap. See KG-3.
- **V5.2.2 (L1) — PARTIAL.** Import paths check the file extension against an allowlist;
  magic-byte/content validation is not performed on every path. See KG-3.
- **V5.2.3 (L2) — PARTIAL.** The story-archive tar import rejects symlink/hardlink members and
  path-escaping members (verified in the 2.100.0 review), but an explicit uncompressed-size /
  file-count (zip-bomb) cap is not enforced. See KG-3.
- **V5.3.1 (L1) — PASS.** Uploaded/imported files are stored under the data dir and served as
  static bytes or relayed; they are never in a code-execution path.
- **V5.3.2 (L1) — PASS.** File paths are built from internal/slugified names;
  `artwork_reader.slugify` strips `../`; readers `resolve()` + `relative_to(root)`.
- **V5.4.1 (L2) — PASS.** Served files use internally-generated names.
- **V5.4.2 (L2) — PARTIAL.** Content-Disposition filenames are largely internal; not all are
  RFC 6266-encoded.
- **V5.4.3 antivirus (L2) — GAP.** No AV scanning of uploaded/imported files. See KG-4
  (accepted for a single-tenant tool handling the operator's own media).

### V6 — Authentication

- **V6.1.1 (L1) — PASS.** Rate-limiting/anti-automation is documented (this file) and
  implemented (`dashboard.py` `_is_rate_limited`, 10 fails / 5 min / IP, cleared on success).
- **V6.1.2, V6.2.11 (L2) — GAP.** No context-specific password blocklist. See KG-5.
- **V6.1.3 (L2) — PASS.** Auth pathways (password + optional TOTP; API keys for automation)
  are documented here with their strengths.
- **V6.2.1 (L1) — PASS.** Minimum length 8 enforced (`dashboard_auth.py:165`). (ASVS
  recommends 15; noted as an optional tightening.)
- **V6.2.2, V6.2.3 (L1) — PASS.** Password change exists and requires the current password.
- **V6.2.4, V6.2.12 (L1/L2) — GAP.** No check against breached / top-N common passwords.
  See KG-5.
- **V6.2.5 (L1) — PASS.** No composition rules (length only).
- **V6.2.6 (L1) — PASS.** Login uses `type="password"`.
- **V6.2.7 (L1) — PASS.** No paste-blocking; password managers work.
- **V6.2.8 (L1) — PARTIAL.** The password is verified as received, **except** that bcrypt
  truncates input at 72 bytes (a library limitation, not app logic). See KG-6.
- **V6.2.9 (L2) — PASS.** Passwords up to bcrypt's 72-byte limit are accepted (≥64 chars OK).
- **V6.2.10 (L2) — PASS.** No forced periodic rotation.
- **V6.3.1 (L1) — PASS.** Brute-force controls (rate limiter) present.
- **V6.3.2 (L1) — PASS.** No default credentials. The default *username* is `admin`, but no
  password exists until the operator sets one at first-run; the endpoint refuses use until then.
- **V6.3.3 (L2) — PASS.** MFA (TOTP) is available.
- **V6.3.4 (L2) — PASS.** No undocumented auth pathways.
- **V6.4.1 (L1) — N/A.** No system-generated initial passwords/activation codes; the operator
  sets the password directly at first run.
- **V6.4.2 (L1) — PASS.** No password hints / security questions.
- **V6.4.3, V6.4.4 (L2) — N/A.** No self-service forgotten-password or MFA-recovery flow that
  could bypass MFA; account recovery is console-level by the box owner (documented).
- **V6.5.1 (L2) — PARTIAL.** TOTP codes are validated with `valid_window=1` but a used code is
  not blocked from reuse within its ~30 s window. See KG-7.
- **V6.5.2, V6.5.4, V6.6.x (L2) — N/A.** No stored lookup/recovery secrets, no OOB/SMS codes.
- **V6.5.3 (L2) — PASS.** The TOTP seed is generated by `pyotp.random_base32()` (CSPRNG).
- **V6.5.5 (L2) — PASS.** TOTP lifetime is the standard 30 s (`valid_window=1` = ±30 s skew).
- **V6.8.x federated IdP (L2) — N/A.** Dashboard login is not federated; no SAML/OIDC assertions.

### V7 — Session Management

- **V7.1.1–V7.1.3 (L2) — PARTIAL.** Timeouts are documented (24 h default / 30 d "remember");
  concurrent sessions are unlimited and there is no federated-session coordination (single
  operator). Documented here rather than enforced.
- **V7.2.1 (L1) — PASS.** Sessions are verified server-side (`itsdangerous` signature check).
- **V7.2.2 (L1) — PASS.** Sessions are dynamically-signed tokens, not static secrets.
- **V7.2.3 (L1) — PASS (by alternative).** The cookie is a **self-contained** signed token
  (see V9), not a random reference token; its signing key is 256-bit (`token_hex(32)`).
- **V7.2.4 (L1) — PARTIAL.** A new token is issued on login; the previous stateless token is
  not individually terminated (see V7.4.1 / KG-8), though a password change now rotates the
  signing key and invalidates all tokens.
- **V7.3.1 (L2) — PARTIAL.** There is an absolute expiry but no separate sliding
  inactivity-timeout. See KG-8.
- **V7.3.2 (L2) — PASS.** Absolute max lifetime enforced (24 h / 30 d).
- **V7.4.1 (L1) — PARTIAL.** Logout clears the cookie client-side; a stateless token, if
  captured, remains valid until expiry (no per-token server-side blocklist). Mitigated by
  `HttpOnly`, short default lifetime, and secret-rotation on password change. See KG-8.
- **V7.4.2 (L1) — N/A.** No account disable/delete (single operator).
- **V7.4.3 (L2) — PASS (fixed this pass).** Password change calls
  `config.rotate_session_secret()`, invalidating all issued sessions.
- **V7.4.4 (L2) — PASS.** Logout is available in the UI.
- **V7.4.5 (L2) — PARTIAL.** An admin can terminate all sessions by rotating the secret
  (password change, or console); no per-session UI.
- **V7.5.1 (L2) — PASS.** Sensitive-attribute changes re-authenticate: password change requires
  the current password; TOTP disable requires password + current code.
- **V7.5.2 (L2) — GAP.** No UI to view/terminate individual active sessions (stateless design).
  See KG-8.
- **V7.6.1 (L2) — N/A.** No RP/IdP relationship.
- **V7.6.2 (L2) — PASS.** Session creation requires an explicit login action.

### V8 — Authorization

- **V8.1.1 (L1) — PASS.** The authorization rule is simple and documented: one authenticated
  role with full access; all endpoints require it (plus the open-instance sensitive-endpoint
  gate).
- **V8.1.2, V8.2.3 field-level (L2) — N/A.** Single role; no field-level differentiation.
- **V8.2.1 (L1) — PASS.** Function-level access is enforced by the auth middleware.
- **V8.2.2 (L1) — N/A (single-tenant).** No cross-user objects exist; IDOR/BOLA is not
  reachable — every object belongs to the one operator.
- **V8.3.1 (L1) — PASS.** Authorization is enforced server-side (middleware), not in the client.
- **V8.4.1 (L2) — N/A.** Not multi-tenant.

### V9 — Self-contained Tokens

The `pp_session` cookie is a self-contained, HMAC-signed `itsdangerous` token.

- **V9.1.1 (L1) — PASS.** The signature is verified before the payload is trusted.
- **V9.1.2 (L1) — PASS.** A fixed HMAC algorithm is used (not JWT; no `alg` header, so no
  algorithm-confusion / `none` attack).
- **V9.1.3 (L1) — PASS.** The signing key comes from the server's stored secret only.
- **V9.2.1 (L1) — PASS.** The `max_age` validity window is enforced on every verification.
- **V9.2.2, V9.2.3, V9.2.4 (L2) — N/A.** A single token type with a single audience (this app);
  no cross-audience reuse to defend against.

### V10 — OAuth and OIDC

PawPoller is an OAuth/OIDC **client** to external publishing platforms (e.g. SoFurry PKCE,
DeviantArt, Threads/Instagram, Pixiv, Tumblr OAuth1). It is **not** an authorization server,
and dashboard login is **not** OIDC.

- **V10.2.1 (L2) — PASS.** Where the app runs an authorization-code flow (SoFurry), it uses
  PKCE (`code_verifier`), consistent with this requirement's client-side protection.
- **V10.1.x consent-handling (L2) — PARTIAL.** Tokens obtained from platforms are stored only
  in the always-on encrypted vault (see V11/V14), i.e. not over-shared with frontend code.
- **All authorization-server requirements (V10.3.*, V10.4.*, V10.5.*, V10.6.*, V10.7.*) — N/A.**
  PawPoller never issues tokens, registers clients, manages redirect-URI allowlists, or runs an
  OIDC provider. It consumes each platform's endpoints per that platform's rules.

### V11 — Cryptography

Cryptographic inventory (also satisfies V11.1.2):

| Purpose | Primitive | Where |
|---|---|---|
| Dashboard password storage | **bcrypt** (per-hash salt) | `config.hash_password` |
| Credential vault (at rest) | **Fernet** = AES-128-CBC + HMAC-SHA256 (encrypt-then-MAC) | `config._encrypt_vault` |
| Session cookie integrity | **HMAC** via `itsdangerous` (256-bit key) | `config.sign_session` |
| API-key storage/compare | **SHA-256** of a high-entropy token | `config.validate_api_key` |
| Random tokens/secrets/seeds | **`secrets`** CSPRNG / `Fernet.generate_key` / `pyotp` | throughout |

- **V11.1.1 (L2) — PARTIAL.** Key lifecycle is documented (SETUP §5.1 + this table): vault key
  source order (operator env → OS keyring → dotfile), rotation on password change for the
  session key. Not a formal NIST SP 800-57 policy.
- **V11.1.2 (L2) — PASS.** The inventory above is maintained here.
- **V11.2.1 (L2) — PASS.** Industry-standard implementations only (`cryptography`/Fernet,
  `bcrypt`, `hashlib`, `secrets`, `itsdangerous`).
- **V11.2.2 (L2) — PARTIAL.** The vault format is versioned (`{"version": 1, …}`), enabling a
  future algorithm migration; primitives are not abstracted behind a pluggable interface.
- **V11.2.3 (L2) — PASS.** ≥128-bit security everywhere (AES-128, 256-bit HMAC key, bcrypt).
- **V11.3.1 (L1) — PASS.** No ECB, no PKCS#1 v1.5 (Fernet uses CBC + HMAC).
- **V11.3.2 (L1) — PASS.** Fernet's AES-CBC-then-HMAC is an approved authenticated-encryption
  construction (GCM is an ASVS *example*, not a mandate).
- **V11.3.3 (L2) — PASS.** Fernet provides authenticated encryption (tamper-evident).
- **V11.4.1 (L1) — PASS (with documented exception).** All PawPoller **security** hashing uses
  bcrypt / SHA-256 / HMAC. `MD5` appears **only** in `clients/pix/client.py`, to compute the
  `X-Client-Hash` header required by Pixiv's mobile-app API handshake — an external-protocol
  requirement, not a control protecting any PawPoller data.
- **V11.4.2 (L2) — PASS.** Passwords use bcrypt (computationally-intensive KDF with salt).
- **V11.4.3 (L2) — PARTIAL.** API-key digests use SHA-256 (≥256-bit). The session HMAC uses
  `itsdangerous`'s default SHA-1 — acceptable for HMAC (collision resistance is not required
  for a MAC) but noted.
- **V11.4.4 (L2) — PASS.** bcrypt provides the key-stretching for password verification; the
  vault key is random (not password-derived), so KDF stretching is N/A there.
- **V11.5.1 (L2) — PASS.** Non-guessable values use `secrets`/`Fernet`/`pyotp` (CSPRNG,
  ≥128-bit).
- **V11.6.1 (L2) — PASS.** Key generation uses approved library routines.

### V12 — Secure Communication

- **V12.1.1, V12.1.2 (L1/L2) — EDGE.** TLS version/cipher selection is the reverse proxy's job
  (Caddy → modern defaults, TLS 1.2/1.3) in the reference deployment.
- **V12.1.3 mTLS (L2) — N/A.** No mTLS.
- **V12.2.1, V12.2.2 (L1) — EDGE.** The browser↔app leg is TLS-terminated by the proxy with a
  publicly-trusted (Let's Encrypt) certificate; the app binds loopback behind it.
- **V12.3.1 (L2) — PASS.** All **outbound** calls to platform APIs use HTTPS via `httpx`.
- **V12.3.2 (L2) — PASS.** `httpx` validates server certificates by default; no `verify=False`
  exists anywhere in the codebase.
- **V12.3.3, V12.3.4 (L2) — N/A/EDGE.** No internal service-to-service hops (single process;
  SQLite is a local file; app↔proxy is loopback).

### V13 — Configuration

- **V13.1.1 (L2) — PASS.** External communication needs are documented: the 16 platform
  APIs, the optional Cloudflare Worker proxy, and the optional Gmail SMTP path.
- **V13.2.1 (L2) — N/A.** No networked backend components requiring service auth (local SQLite).
- **V13.2.2 (L2) — PASS.** The container runs as a non-root user (`pawpoller`).
- **V13.2.3 (L2) — PASS.** No default service credentials; the CF-proxy key etc. are
  operator-supplied.
- **V13.2.4, V13.2.5 (L2) — PARTIAL.** Outbound targets are effectively per-platform clients (an
  implicit allowlist), **but** the thumbnail proxy can fetch arbitrary caller/scrape-supplied
  URLs — see KG-1.
- **V13.3.1 (L2) — PASS.** Secrets live in the always-on encrypted vault; the vault key can be
  held out-of-band via `PAWPOLLER_VAULT_KEY`; `.env`/`settings.json`/vault are gitignored and
  the public-copy leak scanner keeps secrets out of any published tree.
- **V13.3.2 (L2) — PASS.** Secret files are `0600`; the key is in the OS keystore or an
  operator-held env var.
- **V13.4.1 (L1) — PASS.** The app serves only the `frontend/` static tree; `.git` is not in a
  served path or in the runtime image's served root.
- **V13.4.2 (L2) — PASS.** FastAPI debug is off; uvicorn runs without `--reload`/debug.
- **V13.4.3 (L2) — PASS.** `StaticFiles` does not emit directory listings.
- **V13.4.4 (L2) — PASS.** Uvicorn does not implement the `TRACE` method.
- **V13.4.5 (L2) — PASS (fixed this pass).** `/docs`, `/redoc`, `/openapi.json` are disabled
  unless `PAWPOLLER_ENABLE_DOCS=1`.

### V14 — Data Protection

- **V14.1.1, V14.1.2 (L2) — PARTIAL.** Sensitive data is classified here: **platform
  credentials** (highest — encrypted at rest, never logged, never in the public copy);
  analytics data (operator's own); settings (mixed). A formal per-level protection matrix is
  summarized rather than exhaustive.
- **V14.2.1 (L1) — PASS.** Secrets travel in the request body (login) or `Authorization` header
  (API key); the session token is an `HttpOnly` cookie. No credential/token is placed in a URL
  or query string.
- **V14.2.2 (L2) — PARTIAL.** No shared server-side cache holds sensitive data; per-response
  anti-caching is applied on some but not all sensitive responses (see V14.3.2).
- **V14.2.3 (L2) — PASS.** No third-party trackers/analytics; the app is `noindex` and makes no
  outbound calls except to the operator's configured platforms.
- **V14.2.4 (L2) — PARTIAL.** Protection controls (encryption, no-log) are implemented per the
  classification above; retention/access-in-logs policy is summarized, not formalized.
- **V14.3.1 (L1) — PASS.** The session is an `HttpOnly` cookie cleared on logout; no
  authenticated data persists in client storage after logout.
- **V14.3.2 (L2) — PARTIAL.** Some sensitive endpoints send `Cache-Control: no-cache`; a
  blanket `no-store` on all credential-bearing responses is not yet applied. See KG-9.
- **V14.3.3 (L2) — PASS.** Browser storage (localStorage) holds only non-sensitive UI state
  (theme, layout); the session token is a cookie, not localStorage.

### V15 — Secure Coding and Architecture

- **V15.1.1 (L1) — PASS.** Dependency remediation policy documented: `pip-audit` on both
  requirement files at release; CVEs bumped or assessed N/A with rationale (2.100.0).
- **V15.1.2 (L2) — PARTIAL.** `requirements.txt` / `requirements-server.txt` serve as the
  component inventory (pinned, from PyPI); a formal machine-readable SBOM (CycloneDX) is not yet
  generated.
- **V15.1.3, V15.2.2 (L2) — PARTIAL.** Resource-heavy paths (polling, PDF/EPUB generation) run
  off the request thread / on a schedule; documented here, without a formal per-user
  concurrency budget.
- **V15.2.1 (L1) — PASS.** No components past their remediation window (`pip-audit` clean after
  the 2.100.0 bumps).
- **V15.2.3 (L2) — PARTIAL.** The public copy excludes test/dev harnesses (`make_public.py`),
  and the interactive API docs are now off by default (V13.4.5); the diagnostics/testing router,
  where mounted, is auth-gated.
- **V15.3.1 (L1) — PARTIAL.** Endpoints return scoped model fields; single-tenancy means there
  is no other user's field to leak, but responses are not universally field-filtered.
- **V15.3.2 (L2) — PASS.** `httpx` does not follow redirects by default (`follow_redirects`
  defaults to `False`); no untrusted-URL call enables it.
- **V15.3.3 (L2) — PASS.** `save_preferences` and similar use explicit per-field allowlists (no
  blanket object binding), preventing mass assignment.
- **V15.3.4 (L2) — PASS.** The real client IP is taken from the proxy-trusted forwarded header
  and used for rate-limiting / cookie-Secure decisions.
- **V15.3.5 (L2) — PASS.** Inputs are explicitly coerced (`int(...)`, allowlist membership);
  Python's strict typing avoids JS-style juggling.
- **V15.3.6 prototype pollution (L2) — PARTIAL.** Client code does not deep-merge
  attacker-controlled objects into prototypes; not exhaustively audited.
- **V15.3.7 (L2) — PASS.** FastAPI/pydantic resolve each parameter from a single defined source.

### V16 — Security Logging and Error Handling

- **V16.1.1 (L2) — PASS.** Log inventory (this section + the fixes list): auth events, admin
  changes, errors → stdout + rotating file under `LOGS_DIR`.
- **V16.2.1 (L2) — PASS (improved this pass).** Auth log entries now include when/where(IP)/
  who(user)/what(event).
- **V16.2.2 (L2) — PARTIAL.** Timestamps are present and consistently formatted but use the
  server's local time zone rather than explicit UTC. See KG-10.
- **V16.2.3 (L2) — PASS.** Logs go only to the documented sinks (stdout + rotating file).
- **V16.2.4 (L2) — PASS.** A single consistent format string across handlers.
- **V16.2.5 (L2) — PASS.** No credential values are logged (verified: key names/counts only);
  the two truncated OAuth error-body log lines (`pix`, `bsky`) are server-side ERROR/DEBUG only.
- **V16.3.1 (L2) — PASS (fixed this pass).** Login success and every failure mode are logged.
- **V16.3.2 (L2) — PARTIAL.** Middleware 401/API-key rejections are logged; with a single role,
  authorization ≈ authentication.
- **V16.3.3 (L2) — PARTIAL.** Rate-limit trips (a control-bypass signal) are logged.
- **V16.3.4 (L2) — PASS.** The global handler logs unexpected errors with `exc_info`; 5xx are
  logged server-side.
- **V16.4.1 (L2) — PASS (fixed this pass).** `_sanitize_for_log` strips CR/LF/control chars from
  the user-controlled username before logging.
- **V16.4.2 (L2) — PARTIAL.** Log files inherit the data-volume permissions; not
  cryptographically tamper-evident (accepted for a single-host tool).
- **V16.4.3 (L2) — GAP.** No shipping of logs to a separate system. See KG-11 (accepted for
  self-hosted single-box deployment).
- **V16.5.1 (L2) — PASS (fixed this pass).** 5xx responses return a generic message; the real
  detail is logged server-side only.
- **V16.5.2 (L2) — PASS.** External-resource failures degrade gracefully (throttle handling,
  poller try/except-continue) rather than failing the whole app.
- **V16.5.3 (L2) — PASS.** Fails closed: the auth middleware defaults to 401; a vault decrypt
  failure returns no credentials rather than crashing or proceeding.

### V17 — WebRTC

- **V17.1.1–V17.3.2 (L2) — N/A.** PawPoller uses no WebRTC / TURN / DTLS-SRTP / media servers.

---

## Known gaps register

Each gap lists the requirement(s), the honest status, the mitigation already in place, and the
intended remediation (if any). None is a critical/high-severity defect under the single-tenant
threat model.

- **KG-1 — SSRF surface on the thumbnail proxy.** (V1.3.6, V13.2.4/5, PARTIAL) The
  `/api/thumb`, `/api/fa/thumb`, `/api/pix/thumb` endpoints fetch a URL supplied by the caller
  or by scraped platform data, so they could be pointed at an internal address. *Mitigation:*
  these are auth-gated on a configured instance, and the caller is the single authenticated
  operator. *Remediation:* add a scheme (`https` only) + host allowlist / private-IP block to
  the proxy fetchers. Tracked as future hardening. **Update (2.114.0):** the newer
  `POST /api/collections/hash-scan` fetcher already applies this posture — `https`-only, a
  hardcoded public-CDN host-suffix allowlist (`database/image_hash.is_allowed_thumb_url`),
  `follow_redirects=False`, and an 8 MB size cap — so it cannot be aimed at an internal host.
  The pattern is the intended template for retro-fitting the older `/thumb` proxies.
- **KG-2 — Session cookie lacks `__Host-`/`__Secure-` prefix; `Secure` is conditional.**
  (V3.3.1 PARTIAL, V3.3.3 GAP) The cookie is `HttpOnly` + `SameSite=Lax` + `Secure` on HTTPS,
  but not name-prefixed, and `Secure` can't be set on desktop `http://localhost`. *Remediation:*
  emit the cookie as `__Host-pp_session` when served over HTTPS.
- **KG-3 — Upload/import content validation is extension-based; no zip-bomb cap.** (V5.2.1/2/3
  PARTIAL) Imports check extensions and the tar importer rejects traversal/symlink members, but
  magic-byte validation and an uncompressed-size/file-count cap are not universal. *Mitigation:*
  files are the operator's own; never executed; path-traversal is closed. *Remediation:* add
  magic-byte checks and an unpack-size ceiling.
- **KG-4 — No antivirus scanning.** (V5.4.3 GAP, accepted) A single-tenant tool handling the
  operator's own media; AV integration is disproportionate. Documented as accepted.
- **KG-5 — No breached/common-password or context-word blocklist.** (V6.1.2, V6.2.4, V6.2.11,
  V6.2.12 GAP) Length ≥8 is enforced but candidate passwords aren't checked against a breach
  corpus. *Mitigation:* the login is rate-limited and the app is single-operator (no mass
  credential-stuffing target). *Remediation (optional):* a local top-N common-password list, or
  a k-anonymity HaveIBeenPwned range check (adds a network dependency).
- **KG-6 — bcrypt 72-byte truncation.** (V6.2.8 PARTIAL) Passwords longer than 72 bytes are
  silently truncated by bcrypt. *Mitigation:* only affects ≥72-char passwords, which still
  exceed any practical strength. *Remediation (optional):* pre-hash with SHA-256 before bcrypt.
- **KG-7 — TOTP code reuse within its window.** (V6.5.1 PARTIAL) A valid code can be replayed
  within its ~30 s validity. *Mitigation:* 30 s window + login rate-limiting. *Remediation
  (optional):* record the last-accepted TOTP counter and reject reuse.
- **KG-8 — Stateless sessions can't be individually revoked; no inactivity timeout; no session
  list UI.** (V7.2.4, V7.3.1, V7.4.1, V7.4.5, V7.5.2) Inherent to the signed-cookie design.
  *Mitigation:* `HttpOnly`, 24 h default lifetime, and **secret-rotation on password change now
  invalidates all sessions at once**. *Remediation (optional):* move to server-side session
  records (adds state) if per-session control is ever needed.
- **KG-9 — Not all sensitive responses set `Cache-Control: no-store`.** (V14.3.2 PARTIAL)
  *Remediation:* add a blanket `no-store` to credential-bearing API responses.
- **KG-10 — Log timestamps use server-local time, not explicit UTC.** (V16.2.2 PARTIAL)
  *Remediation:* set the log formatter to UTC or include an explicit offset.
- **KG-11 — Logs are local only.** (V16.4.3 GAP, accepted) No remote log shipping — out of
  scope for a single-host self-hosted tool; the operator can point Docker's logging driver at a
  collector if they want it.
- **KG-12 — Formal SBOM.** (V15.1.2 PARTIAL) Pinned requirement files serve as the inventory; a
  CycloneDX SBOM could be generated at release. *Remediation (optional):* add `cyclonedx-py` to
  the release step.

- **KG-13 — The open Instagram image relay is an unauthenticated upload endpoint by
  design (4.7.0).** (V4.1, V5.2, V13 — ACCEPTED, mitigated) `POST /api/ig/relay` lets any
  PawPoller desktop hand this server a picture to host for Meta, because a desktop has no public
  address of its own and the alternative was giving friends a full-access API key. *Mitigation:*
  opt-in only (`ig_relay_open`, server-local, never synced); requires a configured public base;
  body read in chunks under a 12 MB cap; magic-byte check then PIL re-encode to JPEG (the URL can
  only ever serve pixels, never an uploaded file as-is); per-address rate limit (10 per 10 min,
  first `X-Forwarded-For` hop) and a global pending cap (300); unguessable 32-hex tokens with a
  15-minute TTL and sweep; no listing endpoint; the same public GET the operator's own posts have
  used since 2.64.0. *Residual:* the server briefly hosts third-party (SFW, about-to-be-public)
  images at an unguessable URL; a determined party could use it as a 15-minute JPEG host within
  the rate limit. *Remediation (if abused):* turn the switch off — nothing else depends on it.

## How to reproduce / maintain this assessment

- The requirement set is the official ASVS 5.0 release
  (`OWASP_Application_Security_Verification_Standard_5.0.0_en.flat.json`).
- Dependency CVE status: `pip-audit -r requirements.txt` and `-r requirements-server.txt`.
- The security-relevant tests referenced here: `pytest tests/test_auth_gate.py
  tests/test_error_scrub.py tests/test_session_rotation.py tests/test_vault_always_on.py
  tests/test_vault_key.py`.
- Re-run this assessment when touching auth, session, crypto, file handling, or the CSP/headers.
