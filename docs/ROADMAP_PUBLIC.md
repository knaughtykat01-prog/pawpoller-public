# PawPoller Public Release Roadmap

**Status:** Public beta shipped
**Date:** 2026-05-21
**Current version:** 2.25.0
**Latest release:** https://github.com/knaughtykat01-prog/PawPoller/releases/tag/v2.13.8 (master is 22 versions ahead — see HANDOFF.md "CI / release pipeline state" for the tag-drift status)

---

## Vision

PawPoller is a **story-first multi-platform publishing pipeline** for furry fiction writers. Write in Markdown, generate every format (BBCode, HTML, PDF, SquidgeWorld, Styled HTML), publish to 9+ platforms, and track analytics — all from one app.

Two modes:
- **Desktop** — local app (Windows), no server needed. Polls platforms, tracks stats, publishes. Only mode that can post to FurAffinity (datacenter IP blocks).
- **Server + Desktop** — server handles polling/analytics 24/7, desktop syncs settings + handles FA via a queue.

What makes PawPoller different from PostyBirb: PawPoller is built for **writers**, not visual artists. Chaptered posting, per-chapter tags, format conversion, story analytics, drift detection, work skins. PostyBirb posts images with descriptions; PawPoller publishes novels.

---

## What shipped

All must-have and should-have items from the original plan are live in 2.13.8. Condensed status across Phases 8–15:

| Phase | Feature | Shipped | Notes |
|-------|---------|---------|-------|
| 8a | Embedded browser login | 2.12.x | pywebview popup for FA / DA / TW / SF / WS / AO3 / SqW |
| 8b | Manual credential fallback | ✓ | Per-platform settings cards with paste-cookie helpers |
| 8c | Credential vault | 2.12.0 | Fernet + keyring/dotfile key; enable/disable from UI |
| 9a | Setup wizard | 2.12.x | 4-step first-run flow with platform cards |
| 9b | New story wizard | 2.12.1 → 2.13.0 | Title/author/chapters/rating + 9 genre templates + optional file upload |
| 10a | Anchor toolbar | 2.11.0 → 2.13.8 | 10 buttons after the 2.13.7 audit (Title / Sub / Byline / Warning / Disclaimer / FF / Body / → Sent / ← Recv / ☎ Phone) with 1.2 s hover tooltips showing before/after examples |
| 10b | Selective regeneration | 2.11.0 | HTML / BBCode / Styled / SQW / PDF / chapters |
| 10c | Work skin auto-gen | 2.10.0 | `_ensure_work_skin()` on post/edit for AO3 + SqW |
| 10d | Per-platform descriptions | 2.11.0 | Short (IB/SF) + Announcement (Bsky) fields in metadata drawer |
| 10e | Format preview | ✓ | Format tab bar in editor replaces the old dropdown |
| 11a | Cover image upload | ✓ | IB, FA, SF, WS wired end-to-end |
| 11b | Per-chapter thumbnails | 2.12.1 | Metadata drawer slot per chapter, auto-updates story.json |
| 12a | Regen staleness warning | 2.11.0 | Inline warning in Publish Check with "Regenerate now" button |
| 12b | Edit from published stories | 2.11.0 | Edit button on submissions view routes back to editor |
| 12c | Post scheduling | 2.11.0 | Date/time picker + `scheduled_posts` table + cron-like dispatch |
| 12d | Retry queue | 2.11.0 | 1 min / 5 min / 30 min backoff, max 3 attempts |
| 14a | Import from platform | 2.13.0 | IB / SF / FA end-to-end (BBCode/HTML → Markdown). AO3 + SQW still "coming soon" |
| 15a | Repo cleanup + licensing | 2.13.0 | MIT LICENSE, CONTRIBUTING.md, .env.example, `.gitignore` hardening |
| 15b | README + SETUP | 2.13.8 | README trimmed to highlights + Quick Start; new SETUP.md walks through desktop / Docker / web-facing / from-source end-to-end |
| 15c | GitHub Actions CI | 2.13.0 → 2.13.8 | `build.yml` (PyInstaller → zip → release on `v*` tag) + `lint.yml` (ruff + JS syntax) + pinned test deps |
| — | EPUB output (Vellum-style) | 2.17.0 → 2.17.4 | `editor/epub_generator.py` builds EPUB 3.0 with novel-style chapter headings (word-form numbers + drop cap), italic-narration body, phone-screen + text-message styling. Stored in `EPUB/{stem}.epub`, picked up by `_FORMAT_KEY_PATTERNS` and `generate_story_json`. Passes epubcheck 5.1.0 cleanly. |
| — | Mobile-friendly downloads | 2.17.2 → 2.17.4 | `/api/posting/file` allowlists `.epub`; new `/api/posting/archive` streams the whole story folder as a zip. Editor toolbar gets a Downloads ▾ menu (one row per format + "Download all"); published-story page gets a "Download all (zip)" button on the Available Formats card. Workflow: edit on desktop → push → grab the EPUB / PDF on phone. |

Polling-side improvements (outside the original phase plan but shipped as part of the 2.10 – 2.11 push): session expiry recovery across all 11 platforms, N+1 query batching (IB / FA / SQW / AO3 executemany), AO3 rate-limit handling (`Retry-After` + exponential backoff), Telegram error UX, skip-startup-poll.

---

## Open roadmap

### Near-term (completes the 2.13.x line)

- [ ] **AO3 / SquidgeWorld import** — finish the second half of 14a. Scraping + BBCode/HTML → Markdown conversion for OTW-archive stories. Shares most of its code with the existing AO3/SQW poster clients.
- [ ] **Analytics export** — CSV + PNG chart export for story performance. The chart infrastructure (Chart.js) is already in place; needs a "Download CSV" button and an `html2canvas`-style chart → PNG path.
- [ ] **Auto-update mechanism (15d)** — the partial updater (`updater.py`) already checks GitHub for the latest tag and surfaces a notification. Remaining work: one-click "Download + replace .exe + restart" flow using a helper exe (can't self-overwrite a running binary on Windows).
- [ ] **Weasyl end-to-end test** — blocked on account-level verification, not a code issue. Everything else on the Weasyl side is wired.
- [ ] **Deploy 2.13.1+ to the GCP server** — 2.13.1 through 2.13.8 are currently desktop-only. Server still runs 2.13.0. The editor fixes and vault diagnostics are safe to roll out; the anchor-toolbar changes are frontend-only so no server compatibility concerns.

### Medium-term

- [ ] **Cloud sync polish** — Phase 7a shipped the pull/push flow. Outstanding: delta conflict resolution (right now "push" is authoritative), audit log of what synced when, "sync health" badge on the Settings page.
- [ ] **Plugin-ish platform registration** — formalize the per-platform file pattern (`clients/{xx}/`, `polling/{xx}_poller.py`, `database/{xx}_queries.py`, `routes/{xx}_api.py`, `posting/platforms/{xx}.py`) into a contributor-friendly cookiecutter or template command so adding a new platform is a single scaffold step. No dynamic loader — still statically imported.
- [x] **CI test modernization** — already done in 2.13.8. `build.yml` runs `python -m pytest tests/ -v` and `requirements-server.txt` pins `pytest~=8.3` + `pytest-asyncio~=1.3` + `respx~=0.22`. 91 tests vs unittest's 30.
- [ ] **Thumbnail auto-resize** — Pillow is already a dep; fall back to auto-resize when an uploaded cover exceeds a platform's size cap instead of surfacing an error.
- [ ] **Story template library** — beyond the 9 genre presets, let users save their own starting templates (e.g. "my chaptered m/m romance template with these 12 tags pre-selected").

### Coordinated desktop ↔ server (2.14.6 — done)

Closes the dual-polling problem reported by the user: explicit
`setup_mode` ∈ `{standalone, paired_desktop, server}`, polling-owner
gate in `main.py`, server runtime force-stamps `setup_mode = server`.
Wizard rebuilt around a Q1 mode question with a paired-pairing flow
that validates URL+API key and triggers an immediate first-pull.
Settings page gets a Setup Mode panel + Re-run wizard button. See
HANDOFF.md and CHANGELOG `[2.14.6]` for the full story.

- [x] **`polling_owner` gate** — `get_polling_owner(runtime)` returns
  `"local"` or `"server"`; `main.py` skips the 11-thread block when
  the answer is `"server"`.
- [x] **Three-mode setup wizard** — desktop installs answer "Just on
  this computer" or "Pair with my server"; server runtime skips Q1
  entirely.
- [x] **Setting scope tagging** — `SYNC_EXCLUDE` expanded for
  desktop-only fields; tray/startup/notifications hidden on server
  runtime.
- [x] **`auto_sync` server self-protection** — push refuses when
  `setup_mode == "server"` regardless of `posting_server_url`.
- [ ] **Re-pair flow for switching servers** — current Re-run wizard
  works for the standalone↔paired flip but assumes one server at a
  time. If users start running multiple PawPoller boxes a "Switch
  server" affordance with old-pairing-cleanup makes sense.

### Audit-pass debt

Came out of the 2026-04-27 audit pass. Three resolved in 2.14.5,
two left as their own focused passes:

- [x] **N+1 query batching across `database/*_queries.py`** — done in
  2.14.5. `get_*_comparison_snapshots()` across all 11 query files now
  uses one `WHERE submission_id IN (...)` query instead of N SELECTs.
- [x] **Per-poller toast + Telegram notification helper** — done in
  2.14.5. `polling/notifications.py` extracted; 489 lines deleted from
  the 11 pollers, ~150 added in the shared module.
- [x] **Cache `config.get_settings()` at top of route handlers** — done
  in 2.14.5, but most "duplication" turned out to be separate route
  handlers each correctly calling once. Only one true double-call in
  `settings_api.sync_status` — fixed.
- [ ] **`config.py` split** — ~800 lines mixing paths, vault crypto,
  auth helpers, settings I/O, logging setup. Split into
  `paths.py` / `vault.py` / `auth.py` / `settings_io.py`. Has
  boundary decisions that want a focused pass.
- [ ] **Vault key ACL hardening on Windows** — `_secure_file_permissions`
  is a Unix-only no-op, so the `.vault_key` dotfile fallback inherits
  default ACLs on Windows. Mostly theoretical (Windows keyring almost
  always works so the dotfile path is rarely taken), but worth
  closing with DPAPI or `icacls`.
- [x] **Dashboard frontend cache-buster consistency** — already done.
  `index.html` ships with `?v=__APP_VERSION__` on every CSS/JS asset
  and `dashboard.py` splices `config.APP_VERSION` in at request time
  (see the BUG-001 cache-bust fix). Roadmap item kept this open by
  mistake.

### Cloud / hosted access — separate development stream

Surface a "use it in the browser" story without rewriting the world. Three layered options, picked up in order as demand proves out:

**Stage 1 — Demo mode** (low effort, low commitment). Add a `DEMO_MODE=1` flag that short-circuits writes and ships a public sandbox instance hosted by the maintainer. Visitors click around the dashboard/editor, can't save or post. Feeds the "Try it" button on the marketing site. ~1 day of work.

**Stage 2 — One-click deploy (Option A in the site's "Cloud" tier)**. Polish Fly.io / Railway / DigitalOcean deploy manifests so users can spin up their own PawPoller instance with one click. They own their data, they pay the PaaS, the maintainer holds zero credentials. Needs: a `fly.toml` / `railway.json`, a cleaner empty-state first-run flow, a "Deploy your own" button on the site. ~1–2 days of work.

**Stage 3 — Multi-tenant SaaS (Option B)** — *deferred, not planned*. True "sign up and write" hosted service. Requires rewriting every query for per-user scoping, per-user credential vaults, quota/billing, terms of service, security review — and makes the maintainer a credential custodian for everyone who signs up. Gated on real demand + funding for the ongoing liability. Kept open in the roadmap so architectural decisions don't close the door on it (e.g. don't hardcode single-user assumptions in new code), but no work planned.

Design decisions that keep Stage 3 achievable later without breaking Stage 1/2:
- New database tables should carry an optional `owner_id` column (nullable for single-user mode).
- New routes should accept (but not yet require) an authenticated user identity.
- Avoid sprinkling `/app/data/` paths into new code — use `config.DATA_DIR` so it can be per-user later.

### Cross-platform desktop

- [x] **Linux AppImage** — shipped 2.25.0. Single-file distro-independent
  build via PyInstaller + appimagetool. Per-OS shims for the only
  Windows-isms in the codebase: autostart (HKCU registry → XDG
  `.desktop` file under `~/.config/autostart/`); desktop notifications
  (winotify → `notify-send`); pystray backend (`_win32` → `_appindicator`).
  pywebview switched to the Qt backend on Linux so PyInstaller can
  bundle QtWebEngine cleanly (the GTK backend's PyGObject + WebKit2GTK
  system bindings don't bundle well). New `installer/build-appimage.sh`
  drives the AppDir layout + appimagetool invocation; CI's
  `build-linux` job runs on `ubuntu-22.04` (GLIBC 2.35 floor for max
  distro compat). Auto-updater (`updater.py`) picks the Linux
  AppImage asset on Linux runs and replaces in place via `APPIMAGE`
  env var.

- [ ] **macOS native app** — planned. Same shape as Linux work:
  - Reuses the per-OS autostart / notifications / pystray shims
    already in place.
  - pywebview's macOS backend (Cocoa via PyObjC) bundles cleanly via
    PyInstaller `--windowed` → `.app` bundle.
  - WeasyPrint needs Cairo + Pango via Homebrew (`brew install pango
    cairo`); CI runner needs the brew install step.
  - Packaging: `.app` bundle via PyInstaller, then wrap in a `.dmg`
    via `create-dmg` or `dmgbuild`.
  - **Apple Developer cert + notarization is the open question** —
    without one, Gatekeeper blocks first-run on macOS 10.15+, and
    macOS 14+ tightens this further (users have to right-click →
    Open the first time, every install). Costs $99/yr for the cert
    + notarization tooling. Decision pending: ship unsigned and
    accept the friction, or wait until there's enough demand to
    justify the recurring cost.
  - **CI** would add a `build-macos` job on `macos-latest` (Apple
    Silicon) and probably `macos-13` (x86) for a universal build,
    or just ship Apple Silicon and let Rosetta 2 handle x86.
  - Notifications: replace the Linux `notify-send` branch with
    either `pync` (pip-installable) or shell-out to `osascript`.
  - Estimated effort: 2-3 days plus Apple Developer onboarding if
    going signed.

### Long-term / "nice someday"

- [ ] **Goal tracking UI** — set targets for views / faves / comments per story, render progress bars, Telegram notification on hit.
- [ ] **Story performance comparison** — side-by-side charts for multiple stories on one dashboard.
- [ ] **Offline analytics cache** — show last-known stats when a platform is unreachable instead of blank cells.
- [ ] **`.docx` / `.rtf` import** — the new-story wizard accepts these file types today but the conversion is basic; richer Word/RTF → Markdown via `python-docx` / `pandoc` fallback would preserve more formatting.
- [ ] **In-app EPUB viewer** — vendor `epub.js` (~300 KB, MIT-licensed, mobile-friendly), add a small `/epub-viewer.html` page or modal that loads from `/api/posting/file?...&file=EPUB/<name>.epub`, surface a "Preview" button next to the EPUB row in the Downloads dropdown. Removes the round-trip of downloading to a phone just to spot-check formatting. ~30 minutes.
- [ ] **Multi-user mode** — unclear if there's demand. Would need PostgreSQL + per-user credential isolation. Not on the roadmap; flagged so the decision's recorded.

---

## Decisions locked in

These came up in the original plan as open questions and have now been answered by shipping:

1. **pywebview over Electron** — stayed with pywebview. Browser-login worked fine with it; no need for Electron's heavier footprint.
2. **Story format** — Markdown with HTML-comment anchors (`<!-- @title -->` etc.) is the canonical source. `.docx` / `.rtf` are import-only, never the source of truth.
3. **Database** — SQLite (WAL mode). No move to PostgreSQL planned while the app stays single-user.
4. **Name** — stayed "PawPoller" despite the publishing-heavy pivot. Too much muscle memory and existing deploys to rename now.
5. **Plugin system** — platforms share a consistent file pattern but are statically imported. No dynamic loader; contributors add a new platform by following the pattern, not by registering with a runtime API.
6. **Auth mechanism** — session-based for the dashboard (bcrypt + signed cookie + optional TOTP + optional Turnstile + API keys). HTTP Basic was considered and rejected for the richer auth model.

---

## How the public release landed

- **v2.13.0** (2026-04-20) — shipped the full 14a import, genre templates, configurable author, and all the GitHub packaging (15a/b/c).
- **v2.13.1 – v2.13.7** (2026-04-24) — six fixes and polish passes: anchor-toolbar silent-no-op bug, publish-check IndexError for single-piece stories, vault-enable error surfacing, PDF regen diagnostics, PDF header/footer suppression, full-bleed print background, anchor-toolbar audit removing fake anchors.
- **v2.13.8** (2026-04-24) — inline anchor button labels + tooltip pacing + CI pipeline fixes (pytest and respx pinned in `requirements-server.txt`).

What's live right now on GitHub:
- README + SETUP.md + CHANGELOG + documentation_guide + CONTRIBUTING
- GitHub Actions building Windows zip on every `v*` tag push
- MIT license, .env.example, `.gitignore` hardened, no secrets in tracked files
- Single `v2.13.8` release with structured release notes (old v1.x releases from the pre-rename era deleted)

See [`../CHANGELOG.md`](../CHANGELOG.md) for the per-version detail.
