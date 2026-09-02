"""Configuration — loads .env / settings.json and defines paths.

This module is imported early by every other module, so it establishes all
paths, credentials, and tunables in one place.  Two runtime modes are
supported:
  - **Dev mode**: run via `python main.py`, paths are relative to this file.
  - **Frozen mode**: packaged with PyInstaller into a single .exe; bundled
    assets live in a temp directory (sys._MEIPASS) while user data goes
    to %APPDATA%/PawPoller so it persists across updates.
"""

from pathlib import Path
from dotenv import load_dotenv
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import sys
import threading

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path resolution: frozen (PyInstaller) vs dev mode
# ---------------------------------------------------------------------------
# PyInstaller bundles assets into a temporary directory exposed via
# sys._MEIPASS.  The `frozen` attribute is set to True by PyInstaller.
# In dev mode neither attribute exists, so we fall back to the directory
# that contains this source file.  This dual-path pattern lets the same
# code locate icons, templates, and static files regardless of how the
# app was launched.
# ---------------------------------------------------------------------------

def resource_path(relative: str) -> Path:
    """Resolve a path relative to the application root.

    When frozen by PyInstaller, bundled data files live under sys._MEIPASS.
    In normal dev mode this is just the directory containing this file.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # PyInstaller temp extraction directory
    else:
        base = Path(__file__).resolve().parent  # project root in dev mode
    return base / relative


# ── Source directory (code / bundled assets) ──────────────────
# Points to the root of bundled assets (frozen) or the project folder (dev).
SRC_DIR = resource_path(".")

# ── Persistent data directory ─────────────────────────────────
# The APPDATA_DIR split separates *mutable* user data from *immutable*
# bundled code.  In a frozen build the .exe unpacks read-only assets to a
# temp folder that is deleted on exit, so databases, logs, and settings
# must live somewhere persistent -- %APPDATA%/PawPoller is the standard
# Windows location for per-user application data.
# In dev mode everything stays in the project directory for convenience.
#
# Frozen exe  -> %APPDATA%/PawPoller/
# Dev mode    -> ./data, ./logs  (project-local)

_appdata_override = os.environ.get("PAWPOLLER_APPDATA_DIR", "").strip()

if _appdata_override:
    # 3.6.0: explicit override so a source checkout can operate on an INSTALLED
    # install's data — the mirror seed (scripts/mirror_pull.py) has to target
    # %APPDATA%\PawPoller while running from source, and running it as the
    # frozen app instead would mean the GUI holds the database open exactly
    # when the pulled snapshot needs to be swapped in.
    #
    # Point this at the PawPoller folder, NOT at its `data` subfolder — DATA_DIR
    # is derived as APPDATA_DIR/"data" below. Getting that wrong produces a
    # nested `data/data` and an install that silently uses an empty database.
    APPDATA_DIR = Path(_appdata_override)
elif getattr(sys, "frozen", False):
    # Persistent roaming AppData folder survives app updates / reinstalls.
    # On Linux AppImage builds APPDATA is unset — without the XDG fallback
    # the path collapsed to a RELATIVE "PawPoller" dir (CWD-dependent data,
    # and the uninstaller's rm -rf would target a relative path too).
    _appdata = os.environ.get("APPDATA", "")
    if _appdata:
        APPDATA_DIR = Path(_appdata) / "PawPoller"
    else:
        _xdg = os.environ.get("XDG_DATA_HOME", "")
        _base = Path(_xdg) if _xdg else (Path.home() / ".local" / "share")
        APPDATA_DIR = _base / "PawPoller"
else:
    # Dev mode: keep data alongside source for easy inspection
    APPDATA_DIR = Path(__file__).resolve().parent

DATA_DIR = APPDATA_DIR / "data"         # SQLite database and JSON caches
LOGS_DIR = APPDATA_DIR / "logs"         # Rotating log files
DB_PATH = DATA_DIR / "pawpoller.db"     # Main SQLite database
SETTINGS_PATH = DATA_DIR / "settings.json"      # User preferences and credentials

# Create data/log directories on first run (no-op if they already exist)
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Migrate settings.json from old location (APPDATA_DIR) to new (DATA_DIR)
# so it lives on the persistent Docker volume and survives container rebuilds.
_old_settings = APPDATA_DIR / "settings.json"
if _old_settings.exists() and not SETTINGS_PATH.exists():
    import shutil
    shutil.copy2(_old_settings, SETTINGS_PATH)


# ── Goal Metrics Whitelist ─────────────────────────────────────
# Single source of truth for valid metric column names used in SQL queries
# for goal tracking.  Referenced by routes/api.py (create + read) and
# polling/telegram.py (goal completion notifications).  Any metric name
# interpolated into SQL MUST be validated against this set first.
ALLOWED_GOAL_METRICS = frozenset({
    "views", "favorites_count", "comments_count", "watchers",
    "reads", "votes", "likes", "reshares", "downloads", "num_lists",
    "reposts", "retweets", "bookmarks", "quotes", "replies",
})


def _secure_file_permissions(path) -> None:
    """Set file to owner-read/write only (0600) on Unix/Linux.

    No-op on Windows where POSIX permissions don't apply.
    Protects settings.json (which contains credentials) from being
    readable by other users/processes in the Docker container.
    """
    if sys.platform != "win32":
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # Best-effort — don't crash if permissions can't be set


# ── Credential vault (Phase 7b) ────────────────────────────────
# Must be declared BEFORE _load_settings because the module-level
# `_settings = _load_settings()` at import time calls _decrypt_vault()
# when settings.json has credential_mode="local". If these live below
# that init line Python raises NameError at import on vault-mode servers.

VAULT_PATH = DATA_DIR / "settings.vault.json"


def _operator_vault_key() -> bytes | None:
    """An operator-supplied vault key, or None if none is configured.

    Read from ``PAWPOLLER_VAULT_KEY`` (the key itself) or, failing that, from
    the file named by ``PAWPOLLER_VAULT_KEY_FILE`` (e.g. a Docker/K8s secret).
    This lets a server operator hold the key OUT-OF-BAND (secrets manager,
    Docker secret, env) instead of the ``.vault_key`` dotfile that otherwise
    sits next to the ciphertext on the data volume — the only way the vault
    gives real at-rest protection on a server (see docs/SETUP.md §5.1).

    Raises if a key is supplied but malformed, so a typo fails fast at startup
    rather than silently making the vault undecryptable.
    """
    raw = os.environ.get("PAWPOLLER_VAULT_KEY", "").strip()
    if not raw:
        key_path = os.environ.get("PAWPOLLER_VAULT_KEY_FILE", "").strip()
        if key_path:
            try:
                raw = Path(key_path).read_text(encoding="utf-8").strip()
            except OSError as e:
                raise RuntimeError(f"PAWPOLLER_VAULT_KEY_FILE unreadable: {e}") from e
    if not raw:
        return None
    key = raw.encode("ascii")
    try:
        from cryptography.fernet import Fernet
        Fernet(key)  # validates it's a 32-byte url-safe-base64 Fernet key
    except Exception as e:
        raise RuntimeError(
            "PAWPOLLER_VAULT_KEY is not a valid Fernet key. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from e
    return key


def _get_vault_key() -> bytes:
    """Derive or retrieve the encryption key for the credential vault.

    Resolution order:
      1. Operator-supplied key (``PAWPOLLER_VAULT_KEY`` / ``_FILE``) — use this
         on a server so the key is not stored next to the ciphertext.
      2. The system keyring (desktop — key held separately from the vault).
      3. Fallback: a machine-local ``.vault_key`` dotfile in DATA_DIR. This
         sits on the same volume as the vault, so it is only as safe as the
         volume; prefer PAWPOLLER_VAULT_KEY on a server.
    """
    op_key = _operator_vault_key()
    if op_key:
        return op_key
    try:
        import keyring
        key = keyring.get_password("PawPoller", "vault_key")
        if key:
            return key.encode()
        # Generate and store a new key
        from cryptography.fernet import Fernet
        new_key = Fernet.generate_key()
        keyring.set_password("PawPoller", "vault_key", new_key.decode())
        return new_key
    except Exception:
        # Fallback: store key in a dotfile in DATA_DIR
        key_file = DATA_DIR / ".vault_key"
        if key_file.exists():
            return key_file.read_bytes().strip()
        from cryptography.fernet import Fernet
        new_key = Fernet.generate_key()
        key_file.write_bytes(new_key)
        _secure_file_permissions(key_file)
        return new_key


def vault_key_source() -> str:
    """Where the vault key comes from: 'operator' | 'keyring' | 'dotfile'.

    Read-only — mirrors _get_vault_key()'s resolution order without creating
    a key anywhere. Used by the vault status endpoint so the Settings page
    can show which key store protects the credentials.
    """
    try:
        if _operator_vault_key():
            return "operator"
    except RuntimeError:
        # Malformed operator key — _get_vault_key would fail loudly; report
        # the intent (operator) rather than silently claiming a fallback.
        return "operator"
    try:
        import keyring
        if keyring.get_password("PawPoller", "vault_key"):
            return "keyring"
        # keyring importable but no entry yet: _get_vault_key would create
        # one there on first use.
        return "keyring"
    except Exception:
        return "dotfile"


def _encrypt_vault(creds: dict) -> None:
    """Encrypt credential fields to settings.vault.json.

    NOTE: Callers must hold _settings_lock before calling this function.
    """
    from cryptography.fernet import Fernet
    import tempfile
    key = _get_vault_key()
    f = Fernet(key)
    payload = json.dumps(creds).encode("utf-8")
    encrypted = f.encrypt(payload)
    vault_data = {"version": 1, "encrypted": encrypted.decode("ascii")}

    fd, tmp = tempfile.mkstemp(dir=VAULT_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(vault_data, fp, indent=2)
        os.replace(tmp, str(VAULT_PATH))
        _secure_file_permissions(VAULT_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _decrypt_vault() -> dict:
    """Decrypt credential fields from settings.vault.json.

    NOTE: Callers must hold _settings_lock before calling this function.
    """
    if not VAULT_PATH.exists():
        return {}
    try:
        from cryptography.fernet import Fernet
        vault = json.loads(VAULT_PATH.read_text(encoding="utf-8"))
        key = _get_vault_key()
        f = Fernet(key)
        decrypted = f.decrypt(vault["encrypted"].encode("ascii"))
        return json.loads(decrypted)
    except Exception as e:
        logger.error("Failed to decrypt vault: %s", e)
        return {}


# ── Settings.json helpers ─────────────────────────────────────
# settings.json is the single source of truth for user preferences and
# credentials once the app has been configured through the UI.  It uses a
# simple merge-on-write strategy: save_settings() reads the current file,
# overlays the new keys, and writes back, so callers only need to pass the
# keys they want to change.  A threading lock serialises all reads and writes
# to prevent race conditions when multiple routes access settings concurrently.

_settings_lock = threading.Lock()


def _load_settings() -> dict:
    """Load settings.json if it exists, else return empty dict.

    Returns an empty dict (rather than raising) on corrupt/missing files so
    the app can always start with sensible defaults.

    When credential_mode is "local", credential fields are stored in an
    encrypted vault file rather than plaintext settings.json.  This method
    transparently merges decrypted vault contents into the returned dict so
    the rest of the app sees a unified view.

    NOTE: Callers must hold _settings_lock before calling this function.
    """
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}  # Corrupt file -- treat as empty and let next save fix it
    else:
        settings = {}
    # The vault is ALWAYS ON (2.101.0) — merge unconditionally. Cheap when
    # no vault file exists (_decrypt_vault returns {} on a missing file).
    # Plaintext values win nothing here: ensure_vault() migrates them out
    # at startup, and save_settings never writes secrets to plaintext.
    vault_creds = _decrypt_vault()
    if vault_creds:
        settings.update(vault_creds)
    return settings


def save_settings(data: dict) -> None:
    """Merge *data* into settings.json and write.

    Uses read-merge-write so that keys not present in *data* are preserved.
    Thread-safe: acquires _settings_lock for the entire read-modify-write cycle.

    Credential fields are ALWAYS routed to the encrypted vault — plaintext
    settings.json never holds a secret (vault always-on as of 2.101.0).

    Write is atomic: data goes to a temp file first, then os.replace() swaps it
    in.  os.replace() is atomic on the same filesystem, so a crash mid-write
    cannot leave a truncated/corrupt settings.json.
    """
    import tempfile
    with _settings_lock:
        current = _load_settings()
        _before = dict(current)               # snapshot for credential-age stamping
        current.update(data)  # Overlay new values on top of existing ones

        # Proactive credential-age (W): stamp when a platform's credentials are
        # (re)connected, so the UI can warn before a finite-lifetime cookie/token
        # goes stale. credential_set_at is a plain (non-secret) dict → plaintext.
        _changed_creds = _platforms_with_changed_creds(_before, data)
        if _changed_creds:
            from datetime import datetime as _dt, timezone as _tz
            _stamps = dict(current.get("credential_set_at") or {})
            _now = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
            for _p in _changed_creds:
                _stamps[_p] = _now
            current["credential_set_at"] = _stamps

        # Split credentials into vault vs plaintext. is_credential_key()
        # also catches account-namespaced secrets (acct_<id>_<field>), so
        # extra accounts are encrypted like the default. The vault is
        # rewritten even when empty — otherwise deleting the last secret
        # would leave a stale vault that resurrects it on the next load.
        vault_creds = {k: v for k, v in current.items()
                       if is_credential_key(k) and v}
        _encrypt_vault(vault_creds)
        plaintext = {k: v for k, v in current.items()
                     if not is_credential_key(k)}
        # Stamp the mode so a DOWNGRADED build (pre-always-on) still merges
        # the vault instead of silently seeing zero credentials.
        plaintext["credential_mode"] = "local"

        # Write to a temp file in the same directory, then atomically replace.
        # Same-directory ensures same filesystem so os.replace() is atomic.
        fd, tmp_path = tempfile.mkstemp(dir=SETTINGS_PATH.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(plaintext, f, indent=2)
            os.replace(tmp_path, SETTINGS_PATH)
            _secure_file_permissions(SETTINGS_PATH)
        except BaseException:
            # Clean up temp file on any failure (including KeyboardInterrupt)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # Keep the log scrubber's secret list current (2.193.2). PUSHED from here
    # rather than pulled from inside the logging path on purpose: a filter that
    # calls get_settings() does file I/O + a Fernet decrypt per log record, and
    # taking the filter's lock then _settings_lock deadlocks against a thread
    # holding them the other way round. See log_redaction's module docstring.
    _push_log_secrets(current)

    # Fire a debounced push to the cloud server (no-op when not configured
    # or when this save originated from a pull merge — auto_sync handles both).
    _schedule_auto_sync_push()


def _push_log_secrets(settings: dict) -> None:
    """Hand the current secret values to the log scrubber. Never raises."""
    try:
        import log_redaction
        log_redaction.set_secrets(
            log_redaction.secrets_from_settings(settings, is_credential_key))
    except Exception:  # noqa: BLE001 — redaction bookkeeping is never fatal
        pass


def refresh_log_secrets() -> None:
    """Re-read settings and refresh the log scrubber's secret list.

    Called once at startup by the entry points, after logging is configured.
    """
    try:
        _push_log_secrets(get_settings())
    except Exception:  # noqa: BLE001
        pass


def delete_settings_keys(keys: list[str]) -> None:
    """Remove *keys* from settings.json (and vault if in local mode) and write.

    Thread-safe: acquires _settings_lock for the entire read-modify-write cycle.
    Keys that do not exist are silently ignored.
    Uses the same atomic write pattern as save_settings().
    """
    import tempfile
    with _settings_lock:
        current = _load_settings()
        for key in keys:
            current.pop(key, None)

        # Re-split credentials into vault vs plaintext (vault always-on).
        # Unconditional rewrite: deleting the LAST credential must clear the
        # vault too, or the stale ciphertext resurrects it on the next load.
        vault_creds = {k: v for k, v in current.items()
                       if is_credential_key(k) and v}
        _encrypt_vault(vault_creds)
        plaintext = {k: v for k, v in current.items()
                     if not is_credential_key(k)}
        plaintext["credential_mode"] = "local"

        fd, tmp_path = tempfile.mkstemp(dir=SETTINGS_PATH.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(plaintext, f, indent=2)
            os.replace(tmp_path, SETTINGS_PATH)
            _secure_file_permissions(SETTINGS_PATH)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    _schedule_auto_sync_push()


def _schedule_auto_sync_push() -> None:
    """Trigger a debounced auto-sync push if available.

    Imported lazily and exception-swallowed so config.py stays usable when
    auto_sync isn't importable (e.g. unit tests that stub modules).
    """
    try:
        import auto_sync
        auto_sync.schedule_push()
    except Exception:
        pass


def get_settings() -> dict:
    """Public read accessor -- thin wrapper kept separate from the private
    _load_settings() so callers have a clean API and internal helpers can
    evolve independently.  Thread-safe: acquires _settings_lock."""
    with _settings_lock:
        return _load_settings()


# ── Credentials (cascading: settings.json > .env > empty) ────
# Credentials are resolved with a three-tier fallback:
#   1. settings.json  -- written by the UI's settings page at runtime
#   2. .env file      -- developer convenience for local testing
#   3. empty string   -- safe default; pollers skip when creds are blank
# This lets users configure everything through the GUI while still
# allowing developers to drop in a .env for quick local runs.

_BASE_DIR = Path(__file__).resolve().parent
load_dotenv(_BASE_DIR / ".env")  # Load .env as fallback for dev environments

# Why these module-level reads exist alongside get_settings():
# These reads happen once at import time and provide backward compatibility
# for code that imports config.INKBUNNY_USERNAME directly.  Pollers that run
# later should call get_settings() for fresh reads — these module-level values
# are stale snapshots that won't reflect runtime changes made through the UI.
_settings = _load_settings()
# `or` short-circuits: if settings.json has the value, .env is never read
INKBUNNY_USERNAME = _settings.get("username") or os.getenv("INKBUNNY_USERNAME", "")
INKBUNNY_PASSWORD = _settings.get("password") or os.getenv("INKBUNNY_PASSWORD", "")

# ── FurAffinity settings ──
FA_BASE = "https://www.furaffinity.net"          # Main FA website for scraping
FAEXPORT_BASE = "https://faexport.spangle.org.uk"  # Third-party FA API proxy
FA_POLL_INTERVAL_HOURS = 1       # Default hours between FA poll cycles
FA_REQUEST_DELAY_SECONDS = 1.5   # Delay between consecutive FA API requests (rate limiting)
FA_USERNAME = _settings.get("fa_username", "")
# FA uses session cookies (cookie_a and cookie_b) instead of username/password auth
FA_COOKIE_A = _settings.get("fa_cookie_a", "")
FA_COOKIE_B = _settings.get("fa_cookie_b", "")

# ── Weasyl settings ──
WS_REQUEST_DELAY_SECONDS = 1.0  # Rate-limit delay between Weasyl API calls

# ── SoFurry settings ──
SF_REQUEST_DELAY_SECONDS = 1.5  # Rate-limit delay between SoFurry page scrapes (slightly higher for scraping)

# ── SquidgeWorld settings ──
SQW_REQUEST_DELAY_SECONDS = 2.0  # Rate-limit delay between SquidgeWorld page scrapes (higher due to anti-bot)

# ── AO3 settings ──
# Why 12 seconds: 2.22.4 bumped 3s → 6s based on kenalba/ao3-scraper's
# baseline. First live test still hit `Retry-After: 349s` because we'd
# already cooked the per-IP bucket with earlier cycles that day. AO3's
# throttle escalates the longer you're inside the punishment window.
# 12s is "be aggressively generous" — double the external-tool baseline,
# which makes us slower than every comparable scraper and gives the
# bucket comfortable headroom to drain between requests.
# Cost: ~60s extra wall time per ten-work cycle. Still invisible at the
# 240-min polling cadence.
# Bumped 6.0 → 12.0 in 2.22.5 after observing 349s throttle escalation
# on a 6s-pacing cycle.
AO3_REQUEST_DELAY_SECONDS = 12.0

# ── DeviantArt settings ──
DA_REQUEST_DELAY_SECONDS = 2.0  # Rate-limit delay between DeviantArt API requests

# ── Wattpad settings ──
WP_REQUEST_DELAY_SECONDS = 1.0  # Rate-limit delay between Wattpad API requests

# ── Itaku settings ──
IK_REQUEST_DELAY_SECONDS = 1.0  # Rate-limit delay between Itaku API requests

# ── Bluesky settings ──
BSKY_REQUEST_DELAY_SECONDS = 1.0  # Bluesky AT Protocol — generous rate limits

# ── X/Twitter settings ──
TW_REQUEST_DELAY_SECONDS = 2.0  # X GraphQL — aggressive rate limiting, needs higher delay
# The X poll path prefers the gallery-dl CLI (invoked as a separate subprocess —
# see clients/tw/gallerydl.py) and falls back to the built-in GraphQL scrape when
# gallery-dl is absent. This caps how long we wait on that subprocess per cycle.
# 480s (8 min): from a datacenter IP X often 429s the timeline endpoint and
# gallery-dl correctly waits for X's reset (~6 min observed) before fetching. The
# cap must exceed that wait, else we kill gallery-dl mid-wait and fall back to a
# GraphQL path that 429s on the same rate limit. 8 min rides out a typical reset
# so gallery-dl succeeds; acceptable to block one account that long at the 12h cadence.
TW_GALLERYDL_TIMEOUT_SECONDS = 480
# Shared cross-account cap for X requests PawPoller makes directly (GraphQL scrape
# + official API), enforced as a sliding window by polling/rate_limit.py. X's
# timeline rate limit is per-IP and shared across accounts, so polling several
# back-to-back must stay under it globally. 15 req / 30 s == 1 req / 2 s averaged.
TW_RATE_LIMIT_REQUESTS = 15
TW_RATE_LIMIT_WINDOW_SECONDS = 30
# Round-robin cap for X polling: poll only the N least-recently-polled X
# accounts per cycle, rotating the rest to later cycles (polling/roundrobin.py).
# The datacenter IP throttles after ~2 X account-scrapes per window, so a
# back-to-back poll of 3+ accounts 429s the tail; batch 2 stays inside that
# budget. 0 disables (poll all). Overridable per-user via the tw_roundrobin_batch
# setting. Only X is round-robined — other platforms poll every account.
TW_ROUNDROBIN_BATCH = 2
# X account STAGGER (polling/rate_limit.py): when polling ALL X accounts in one
# cycle (tw_roundrobin_batch=0), space them so the per-IP throttle never trips.
# The IP tolerates ~2 account-scrapes per window then needs a >8-min reset, so we
# poll in bursts of TW_ACCOUNT_STAGGER_EVERY (2) and sleep TW_ACCOUNT_STAGGER_SECONDS
# (480 = 8 min) between bursts — long enough for a fresh window, so every account
# stays on the free gallery-dl path instead of the paid fallback. The first burst
# has no wait, so a 1–2 account cycle (or round-robin batch 2) is never slowed.
# 0 disables. Overridable per-user via tw_account_stagger_seconds. X-only.
TW_ACCOUNT_STAGGER_SECONDS = 480
TW_ACCOUNT_STAGGER_EVERY = 2

# ── Mastodon settings ──
MAST_REQUEST_DELAY_SECONDS = 0.5  # Mastodon REST — per-instance limits are generous

# ── Tumblr settings ──
TUM_REQUEST_DELAY_SECONDS = 0.5  # Tumblr v2 API — generous rate limits for read

# ── Pixiv settings ──
PIX_REQUEST_DELAY_SECONDS = 1.0  # Pixiv app-API — be gentle, it rate-limits hard

# ── Threads settings ──
THR_REQUEST_DELAY_SECONDS = 1.0  # Threads Graph API — per-post insights, go gentle

# ── Instagram settings ──
IG_REQUEST_DELAY_SECONDS = 1.0  # Instagram Graph API — one /insights call per post, go gentle

# ── e621 settings ──
E621_REQUEST_DELAY_SECONDS = 1.0  # e621 REST API — hard limit 2 req/s, docs ask ~1 req/s

# ── Settings sync (Phase 7a) ────────────────────────────────

CREDENTIAL_FIELDS = frozenset({
    # Inkbunny
    "username", "password",
    # FurAffinity
    "fa_cookie_a", "fa_cookie_b",
    # Weasyl
    "ws_api_key",
    # SoFurry — 3.4.0 moved to an official-API Personal Access Token. The old
    # login fields stay listed so any value left in an existing vault is still
    # treated as sensitive until the migration clears it.
    "sf_api_token", "sf_username", "sf_password", "sf_session_cookies",
    # SquidgeWorld
    "sqw_username", "sqw_password",
    "sqw_author_username", "sqw_author_password",
    # AO3
    "ao3_username", "ao3_password", "ao3_session_cookie",
    # DeviantArt
    "da_cookie", "da_client_secret", "da_refresh_token",
    # Itaku
    "ik_auth_token",
    # Bluesky
    "bsky_identifier", "bsky_app_password",
    # X/Twitter
    "tw_auth_token", "tw_ct0",
    # X/Twitter official API v2 Bearer token (opt-in official-API poll backend)
    "tw_api_bearer_token",
    # Mastodon
    "mast_access_token",
    # Tumblr (api_key = OAuth consumer key for read; the rest enable posting)
    "tum_api_key", "tum_consumer_secret", "tum_oauth_token", "tum_oauth_token_secret",
    # Pixiv
    "pix_refresh_token",
    # Threads
    "thr_access_token",
    # Instagram
    "ig_access_token",
    # e621 (username is a non-secret identity field → stays plaintext)
    "e621_api_key",
    # FurryNetwork (OAuth password grant; login email stays plaintext identity)
    "fn_password", "fn_refresh_token", "fn_access_token",
    # Furbooru (Philomena) — optional API key; username stays plaintext identity
    "fbr_api_key",
    # CF proxy
    "cf_worker_url", "cf_worker_key",
    # Dashboard auth
    "auth_password_hash", "auth_api_keys",
    "auth_session_secret", "auth_totp_secret",
    "auth_totp_enabled", "auth_totp_pending_secret",
    "auth_totp_backup_codes",   # 2FA recovery codes (SHA-256 hashes) — gap-wave-4
    "dashboard_password", "dashboard_user",
    # Integrations
    "telegram_bot_token", "telegram_chat_id",
    # Telegram channel posting (Posts module) — bot token is secret; the channel
    # (@name) stays plaintext identity. Falls back to telegram_bot_token if unset.
    "tg_bot_token",
    # Weekly email digest — SMTP app password (host/user/from/recipients stay
    # plaintext as non-secret config; only the password is vaulted).
    "smtp_password",
    "github_pat",
    "turnstile_site_key", "turnstile_secret_key",
    # Server ↔ desktop
    "posting_server_url", "posting_server_api_key",
})

SYNC_EXCLUDE = frozenset({
    "credential_mode",
    "minimize_to_tray",
    # Desktop-only — never leak to the server settings dump
    "run_on_startup",
    # Setup mode is decided per-device (server is always "server"; desktop
    # is "standalone" or "paired_desktop"). Syncing it would let one side
    # overwrite the other's mode, which is exactly what we don't want.
    "setup_mode",
    # ── Who may log in: NEVER syncs, in either direction (3.5.3) ──
    # These decide *access to the instance*, so they must stay per-device.
    # `auth_session_secret` was already excluded — the session secret was
    # recognised as per-device, but the credentials it protects were not,
    # which left the actual hole:
    #
    #   * `auth_api_keys` — a paired desktop's push replaced the SERVER's key
    #     list with its own stale copy, silently revoking the key the sync was
    #     authenticating with. Observed live: pairing worked, the desktop
    #     pushed, and every subsequent request 401'd with the UI still
    #     reporting "nothing newer to pull". It is also a persistence vector,
    #     since a push could ADD a key granting ongoing server access.
    #   * `auth_password_hash` / `auth_username` — a push would overwrite the
    #     server's dashboard login with the desktop's, locking the operator
    #     out of their own server (or silently changing who can get in).
    #
    # Credentials for third-party PLATFORMS deliberately still sync — that is
    # the whole point of pairing. What must not sync is who can log in HERE.
    "auth_api_keys",
    "auth_password_hash",
    "auth_username",
    "auth_session_secret",
    "auth_2fa_secret",
    "auth_2fa_enabled",
    "auth_backup_codes",
    # ── Filesystem locations: they describe THIS box, not the install ──
    # `C:\Users\...\Archives` means nothing on a Linux VM and `/app/data/artwork`
    # means nothing on Windows, yet all four of these cross the sync today. The
    # only thing stopping a synced foreign path from taking effect is that the
    # os.path.isdir() check downstream happens to fail on it — luck, not design,
    # and the failure mode when the luck runs out is that a machine writes its
    # archive somewhere nobody looks. `auto_backup_dir` is the worst of them:
    # a wrong value there points the nightly backup at a path that may not exist.
    "posting_story_archive_path",
    "artwork_archive_path",
    "auto_backup_dir",
    # Public base URL used to build image links for Instagram's fetcher — it is
    # whatever THIS instance is reachable at, so a desktop's value (typically
    # localhost) would make the server hand Meta an unfetchable URL.
    "ig_public_base_url",
})


# ── Multi-account credential resolution ───────────────────────
# Each platform's *default* account keeps using the legacy flat settings keys
# (``username``/``password``, ``fa_username``/``fa_cookie_a``…) so existing
# installs need zero credential migration. Additional accounts store the SAME
# canonical fields under an ``acct_<account_id>_<field>`` prefix. The resolver
# below hands callers a creds dict keyed by the canonical field names regardless
# of which account it is, so clients/posters don't care whether they're the
# default account or the fifth one.
#
# PLATFORM_CREDENTIAL_FIELDS lists, per platform, the canonical settings keys
# that make up an account's identity + secrets. Secret-ness (vault routing) is
# still decided by membership in CREDENTIAL_FIELDS above — non-secret identity
# fields like ``fa_username`` stay in plaintext exactly as they do today.
PLATFORM_CREDENTIAL_FIELDS = {
    "ib": ["username", "password"],
    "fa": ["fa_username", "fa_cookie_a", "fa_cookie_b"],
    "ws": ["ws_username", "ws_api_key"],
    "sf": ["sf_api_token", "sf_display_name"],
    "sqw": ["sqw_username", "sqw_password", "sqw_target_user",
            "sqw_author_username", "sqw_author_password"],
    "ao3": ["ao3_username", "ao3_password", "ao3_target_user", "ao3_session_cookie"],
    "da": ["da_cookie", "da_target_user",
           "da_client_id", "da_client_secret", "da_refresh_token"],
    "wp": ["wp_target_user"],
    "ik": ["ik_target_user", "ik_auth_token"],
    "bsky": ["bsky_identifier", "bsky_app_password"],
    "tw": ["tw_auth_token", "tw_ct0", "tw_target_user", "tw_api_bearer_token"],
    "mast": ["mast_instance_url", "mast_access_token"],
    "tum": ["tum_api_key", "tum_blog", "tum_consumer_secret",
            "tum_oauth_token", "tum_oauth_token_secret"],
    "pix": ["pix_refresh_token", "pix_user_id"],
    "thr": ["thr_access_token", "thr_user_id"],
    "ig": ["ig_access_token", "ig_user_id"],
    "e621": ["e621_username", "e621_api_key"],
    # Telegram channel (Posts-module broadcast target; post-only, not polled).
    "tg": ["tg_bot_token", "tg_channel"],
    # FurryNetwork (poll+post gallery). Email + password → OAuth token/refresh.
    "fn": ["fn_username", "fn_password", "fn_refresh_token", "fn_access_token"],
    # Furbooru (Philomena booru; poll-only). Username + optional API key.
    "fbr": ["fbr_username", "fbr_api_key"],
}

# ── Proactive credential-age tracking (backlog W) ──────────────────
# Cookie/token logins that expire on a schedule AND don't auto-refresh benefit
# from a heads-up BEFORE they die (today's status only goes red once a poll/post
# has already failed). Values are conservative soft lifetimes in DAYS — we warn
# as the stored credential's age approaches them. Deliberately narrow:
#   • Instagram / Threads are OMITTED — their tokens auto-refresh on the session-
#     check cadence, so a 'valid' session is the truth; age would cry wolf.
#   • Mastodon / Tumblr / Bluesky / e621 tokens/app-passwords/keys don't expire.
# Only cookie sessions with no refresh path are tracked, so a warning is real.
CREDENTIAL_SOFT_TTL_DAYS = {
    "tw": 30,   # X (Twitter) auth_token / ct0 cookie session
    "fa": 45,   # FurAffinity cookies (a, b)
    "da": 45,   # DeviantArt cookie
}


def _platforms_with_changed_creds(before: dict, data: dict) -> list:
    """Platforms whose credential fields were just SET/changed to a non-empty
    value in this save (i.e. a (re)connect) — for credential-age stamping.
    Only the default account's bare fields are considered; that's the account
    whose expiry the proactive warning is about."""
    out = []
    for platform, fields in PLATFORM_CREDENTIAL_FIELDS.items():
        for f in fields:
            if f in data and data.get(f) and (data.get(f) or "") != (before.get(f) or ""):
                out.append(platform)
                break
    return out


def _credential_configured(code: str, settings: dict) -> bool:
    """A tracked platform is 'configured' if any of its credential fields is set."""
    return any(settings.get(f) for f in PLATFORM_CREDENTIAL_FIELDS.get(code, []))


def backfill_credential_stamps(settings: dict | None = None) -> bool:
    """Give every configured-but-unstamped tracked platform a set_at of NOW, once.

    Existing installs connected their accounts before credential-age tracking
    existed, so they have no stamp. We can't know the real age, and assuming
    'old' would cry wolf — so we start the clock at now (idempotent; only fills
    gaps). Returns True if anything was written. Called lazily by the report
    endpoint so tracking begins the first time the user looks."""
    s = settings if settings is not None else get_settings()
    stamps = dict(s.get("credential_set_at") or {})
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    changed = False
    for code in CREDENTIAL_SOFT_TTL_DAYS:
        if code not in stamps and _credential_configured(code, s):
            stamps[code] = now
            changed = True
    if changed:
        save_settings({"credential_set_at": stamps})
    return changed


def credential_age_report(settings: dict | None = None) -> list:
    """Per-platform credential-age report for the tracked (finite-lifetime, no-
    refresh) platforms. Each entry: ``{code, set_at, age_days, ttl_days, level}``.

    level: 'ok' (<70% of ttl) · 'aging' (70–99%) · 'stale' (>=100%). Only
    CONFIGURED platforms appear."""
    from datetime import datetime, timezone
    s = settings if settings is not None else get_settings()
    stamps = s.get("credential_set_at") or {}
    now = datetime.now(timezone.utc)
    out = []
    for code, ttl in CREDENTIAL_SOFT_TTL_DAYS.items():
        if not _credential_configured(code, s):
            continue
        set_at = stamps.get(code)
        age_days, level = None, "ok"
        if set_at:
            try:
                dt = datetime.strptime(set_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age_days = max(0, int((now - dt).total_seconds() // 86400))
                frac = (age_days / ttl) if ttl else 0
                level = "stale" if frac >= 1 else ("aging" if frac >= 0.7 else "ok")
            except ValueError:
                pass
        out.append({"code": code, "set_at": set_at, "age_days": age_days,
                    "ttl_days": ttl, "level": level})
    return out


# Matches an account-namespaced settings key: acct_<id>_<canonical_field>.
_ACCT_KEY_RE = re.compile(r"^acct_(\d+)_(.+)$")


def is_credential_key(key: str) -> bool:
    """True if *key* names a secret that belongs in the encrypted vault.

    Covers both the legacy flat keys (membership in CREDENTIAL_FIELDS) and the
    account-namespaced form ``acct_<id>_<field>`` whose underlying canonical
    field is itself a secret. This keeps extra-account secrets encrypted exactly
    like the default account's, while leaving non-secret identity fields (and
    their namespaced variants, e.g. ``acct_5_fa_username``) in plaintext.
    """
    if key in CREDENTIAL_FIELDS:
        return True
    m = _ACCT_KEY_RE.match(key)
    return bool(m and m.group(2) in CREDENTIAL_FIELDS)


def account_setting_key(account_id: int, field: str, is_default: bool) -> str:
    """Return the settings key holding *field* for the given account.

    The default account uses the bare canonical field; others are namespaced.
    """
    return field if is_default else f"acct_{account_id}_{field}"


def resolve_account_credentials(platform: str, account_id: int,
                                is_default: bool, settings: dict | None = None) -> dict:
    """Return {canonical_field: value} for one account, no DB access.

    Pollers/posters that already hold the account row should call this directly.
    """
    fields = PLATFORM_CREDENTIAL_FIELDS.get(platform, [])
    if settings is None:
        settings = get_settings()
    return {f: settings.get(account_setting_key(account_id, f, is_default), "")
            for f in fields}


def get_account_credentials(account_id: int) -> dict:
    """Return {canonical_field: value} for *account_id*, looking up its row.

    Convenience wrapper around :func:`resolve_account_credentials` for callers
    that have only an account_id. Returns {} for an unknown account.
    """
    try:
        from database import db as _db, accounts as _accts
        conn = _db.get_connection()
        try:
            acct = _accts.get_account(conn, account_id)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("get_account_credentials(%s) lookup failed: %s", account_id, e)
        return {}
    if not acct:
        return {}
    return resolve_account_credentials(
        acct["platform"], account_id, bool(acct["is_default"]))


# ── Setup mode + polling ownership ────────────────────────────
# `setup_mode` tells each instance what role it plays. Three values:
#
#   "standalone"     — Desktop only; no server. Polls locally.
#   "paired_desktop" — Desktop running alongside a remote server; pulls
#                      settings from the server, defers polling to it,
#                      but can still post stories from the local archive.
#   "server"         — Headless container. Always polls.
#
# When `setup_mode` is unset (existing installs upgraded from < 2.14.6),
# we infer from runtime + presence of `posting_server_url` so behaviour
# matches what the user already had.

SETUP_MODE_STANDALONE = "standalone"
SETUP_MODE_PAIRED = "paired_desktop"
SETUP_MODE_SERVER = "server"
VALID_SETUP_MODES = frozenset({SETUP_MODE_STANDALONE, SETUP_MODE_PAIRED, SETUP_MODE_SERVER})


def get_polling_owner(runtime: str) -> str:
    """Return ``"local"`` if this process should run the poll loop, else ``"server"``.

    ``runtime`` is the entry point: ``"desktop"`` (main.py) or
    ``"server"`` (server.py). The server is always the polling owner on
    its own box — there's no other PawPoller that could run there. The
    desktop is the polling owner only when it knows it's standalone.

    Inference rules for unset ``setup_mode`` (back-compat with installs
    that predate the mode setting):

    * Desktop with ``posting_server_url`` set → assume paired; server polls.
    * Desktop with no server URL → assume standalone; desktop polls.
    """
    if runtime == "server":
        return "local"  # this process *is* the server, it polls
    settings = get_settings()
    mode = settings.get("setup_mode")
    if mode == SETUP_MODE_PAIRED:
        return "server"
    if mode == SETUP_MODE_STANDALONE:
        return "local"
    if mode == SETUP_MODE_SERVER:
        # Defensive: a desktop install shouldn't have mode=server, but if
        # it somehow does we don't want it polling and racing the real one.
        return "server"
    # No mode set — fall back to the implicit pairing signal.
    if settings.get("posting_server_url") and settings.get("posting_server_api_key"):
        return "server"
    return "local"


def get_credential_mode() -> str:
    """Always 'local' — the credential vault is unconditional as of 2.101.0.

    Kept (rather than deleted) because callers report it for display and a
    downgraded build reads the stored key; there is no longer a 'cloud'
    plaintext mode to return.
    """
    return "local"


def ensure_vault() -> int:
    """Startup guard: the credential vault is ALWAYS ON (2.101.0).

    If settings.json still holds plaintext credential values — a pre-2.101.0
    install upgrading, a hand-edited file, or a restore from an old backup —
    migrate them into the vault now. Also stamps ``credential_mode: local``
    on files that predate the vault so downgraded builds keep working.
    Idempotent; costs one raw file read when there is nothing to do.

    Returns the number of fields migrated (0 when already clean).
    """
    with _settings_lock:
        raw = {}
        if SETTINGS_PATH.exists():
            try:
                raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return 0  # unreadable — let the normal load path handle it
        plaintext_secrets = any(is_credential_key(k) and v for k, v in raw.items())
        stamped = raw.get("credential_mode") == "local"
    if plaintext_secrets or not stamped:
        # Outside the lock: migrate_to_local_vault() takes _settings_lock itself.
        return migrate_to_local_vault()
    return 0


def migrate_to_local_vault() -> int:
    """Move any plaintext credentials from settings.json into the vault.

    The always-on migration path (called via ensure_vault() at startup).
    Returns count of fields migrated.
    """
    with _settings_lock:
        settings = _load_settings()
        creds = {k: v for k, v in settings.items() if is_credential_key(k) and v}
        if not creds:
            # Still switch mode even if no creds to migrate
            settings["credential_mode"] = "local"
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=SETTINGS_PATH.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(settings, fp, indent=2)
                os.replace(tmp, str(SETTINGS_PATH))
                _secure_file_permissions(SETTINGS_PATH)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return 0
        _encrypt_vault(creds)
        # Remove credential fields from plaintext settings
        for k in creds:
            settings.pop(k, None)
        settings["credential_mode"] = "local"
        # Write cleaned settings
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=SETTINGS_PATH.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(settings, fp, indent=2)
            os.replace(tmp, str(SETTINGS_PATH))
            _secure_file_permissions(SETTINGS_PATH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return len(creds)


def migrate_to_cloud() -> int:
    """BREAK-GLASS ONLY: decrypt the vault back into plaintext settings.json.

    No longer reachable from the UI or API — the vault is always-on. Kept as
    a console escape hatch for key-store emergencies (e.g. the OS keyring is
    about to be lost and you want to dump credentials while they still
    decrypt): ``python -c "import config; config.migrate_to_cloud()"``.
    Note the app will re-encrypt everything via ensure_vault() on next start.

    Returns count of fields migrated.
    """
    with _settings_lock:
        creds = _decrypt_vault()
        settings = _load_settings()
        if creds:
            settings.update(creds)
        settings["credential_mode"] = "cloud"
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=SETTINGS_PATH.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(settings, fp, indent=2)
            os.replace(tmp, str(SETTINGS_PATH))
            _secure_file_permissions(SETTINGS_PATH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    # Remove vault file
    if VAULT_PATH.exists():
        VAULT_PATH.unlink()
    return len(creds) if creds else 0


def get_settings_for_sync() -> tuple[dict, float]:
    """Return (settings_dict, mtime) for the sync endpoint.

    Excludes keys in SYNC_EXCLUDE.
    """
    with _settings_lock:
        data = _load_settings()
    mtime = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else 0
    out = {k: v for k, v in data.items() if k not in SYNC_EXCLUDE}
    # Carry the account registry (DB state, not a setting) so desktop↔server
    # agree on which accounts exist. Guarded — never break sync over this.
    try:
        from database import db as _db, accounts as _accts, personas as _personas
        conn = _db.get_connection()
        try:
            out["_accounts_manifest"] = _accts.get_manifest(conn)
            out["_personas_manifest"] = _personas.get_manifest(conn)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("accounts/personas manifest export skipped: %s", e)
    return out, mtime


def merge_synced_settings(incoming: dict, client_timestamp: float | None = None) -> dict:
    """Merge incoming settings from a sync push.

    Applies SYNC_EXCLUDE filtering, then merges into current settings.
    Returns the merged result.
    """
    filtered = {k: v for k, v in incoming.items() if k not in SYNC_EXCLUDE}
    # The account registry rides the sync channel but is DB state, not a
    # setting — apply it to the accounts table (additive, never deletes) and
    # strip it so it isn't persisted into settings.json.
    personas_manifest = filtered.pop("_personas_manifest", None)
    accounts_manifest = filtered.pop("_accounts_manifest", None)
    if personas_manifest is not None or accounts_manifest is not None:
        try:
            from database import db as _db, accounts as _accts, personas as _personas
            conn = _db.get_connection()
            try:
                # Personas BEFORE accounts so account→persona references land
                # after the persona rows exist. Both additive, never delete.
                if personas_manifest is not None:
                    _personas.apply_manifest(conn, personas_manifest)
                if accounts_manifest is not None:
                    _accts.apply_manifest(conn, accounts_manifest)
            finally:
                conn.close()
        except Exception as e:
            logger.warning("accounts/personas manifest import skipped: %s", e)
    if not filtered:
        return get_settings()
    save_settings(filtered)
    return get_settings()


# ── App metadata ──
APP_VERSION = "4.0.2"

# ── Inkbunny API settings ──
INKBUNNY_API_BASE = "https://inkbunny.net"     # Inkbunny API root URL
POLL_INTERVAL_HOURS = 1                        # Default hours between IB poll cycles
REQUEST_DELAY_SECONDS = 1.0                    # Delay between general IB API requests
FAVE_REQUEST_DELAY_SECONDS = 0.5               # Shorter delay for fave lookups (lighter endpoint)
COMMENT_REQUEST_DELAY_SECONDS = 1.0            # Delay between comment-fetching requests
SUBMISSION_BATCH_SIZE = 100                    # Max submissions fetched per API page request

# ── Dashboard (local web server) ──
DASHBOARD_HOST = "127.0.0.1"  # Localhost only -- not exposed to the network
DASHBOARD_PORT = 8420          # Arbitrary high port unlikely to conflict

# Trusted proxy IPs for uvicorn's X-Forwarded-* handling. Default 127.0.0.1
# (safe for desktop / direct binding). Behind a reverse proxy that terminates
# TLS (e.g. Caddy for pawpoller.syncopates.app), set PAWPOLLER_FORWARDED_IPS=*
# so request.url.scheme reflects the real HTTPS connection — the dashboard
# session cookie's Secure flag (routes/dashboard_auth.py) and per-client rate
# limiting depend on it. Only widen this when actually behind a trusted proxy.
DASHBOARD_FORWARDED_IPS = os.environ.get("PAWPOLLER_FORWARDED_IPS", "127.0.0.1")

# ── Stat offsets ──
# Optional manual reconciliation for the Inkbunny "All accounts" totals. The IB
# API only returns data for *public* submissions, so if YOU personally have
# deleted or private submissions, the API totals read lower than your real IB
# dashboard. Bump these to close that gap for your own instance — but they must
# ship at 0, otherwise every fresh install shows phantom stats (e.g. "301 views"
# with nothing uploaded). Applied only to the aggregate view (database/queries.py).
VIEWS_OFFSET = 0
FAVORITES_OFFSET = 0
COMMENTS_OFFSET = 0


# ── Dashboard Auth Helpers ────────────────────────────────────
# Bcrypt password hashing, session cookie signing, and API key validation
# for the self-hosted dashboard authentication system.  These are used by
# routes/dashboard_auth.py and the session middleware in dashboard.py.

def hash_password(password: str) -> str:
    """Hash a password with bcrypt.  Returns the hash as a UTF-8 string."""
    import bcrypt
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Check a plaintext password against a bcrypt hash."""
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


_session_secret_cache: str | None = None


def get_or_create_session_secret() -> str:
    """Return the session signing secret, creating one on first call.

    The secret is a 32-byte hex string stored in settings.json.  It is
    generated once and reused across restarts so that existing session
    cookies remain valid.  Regenerating it would log out all users.
    Cached in memory since it never changes after creation.
    """
    global _session_secret_cache
    if _session_secret_cache is not None:
        return _session_secret_cache
    settings = get_settings()
    secret = settings.get("auth_session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        save_settings({"auth_session_secret": secret})
    _session_secret_cache = secret
    return secret


def rotate_session_secret() -> None:
    """Generate a new session signing secret, invalidating ALL existing sessions.

    Called after a password change so that any other logged-in sessions (and a
    stolen session cookie) are terminated — the stateless signed cookie can't be
    revoked individually, so rotating the signing key is how we force re-auth
    everywhere (ASVS 5.0 V7.4.3). The caller's own cookie is invalidated too, so
    the client must log in again.
    """
    global _session_secret_cache
    new_secret = secrets.token_hex(32)
    save_settings({"auth_session_secret": new_secret})
    _session_secret_cache = new_secret


_SESSION_MAX_AGE_SHORT = 86400        # 24 hours (default)
_SESSION_MAX_AGE_LONG = 30 * 86400   # 30 days ("remember me")


def sign_session(payload: dict) -> str:
    """Sign a session payload and return the cookie value.

    The payload should include a ``"r": True`` flag for "remember me"
    sessions so verify_session() can apply the correct max_age.
    """
    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(get_or_create_session_secret())
    return s.dumps(payload)


def verify_session(cookie: str) -> dict | None:
    """Verify a signed session cookie.  Returns the payload dict or None.

    Tries the short max_age first, then the long max_age.  The ``"r"``
    (remember) flag in the payload determines which expiry applies:
    sessions without ``"r": True`` expire after 24 hours; sessions with
    it expire after 30 days.
    """
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    s = URLSafeTimedSerializer(get_or_create_session_secret())
    # First try with the long max_age (universal upper bound)
    try:
        payload = s.loads(cookie, max_age=_SESSION_MAX_AGE_LONG)
    except (BadSignature, SignatureExpired):
        return None
    # If not a "remember me" session, enforce the short max_age
    if not payload.get("r"):
        try:
            s.loads(cookie, max_age=_SESSION_MAX_AGE_SHORT)
        except SignatureExpired:
            return None
        except BadSignature:
            return None
    return payload


_auth_required_cache: bool | None = None


def is_dashboard_auth_required() -> bool:
    """Return True if dashboard authentication is configured.

    Auth is required when either:
      - A bcrypt password hash exists (new system), or
      - A legacy plaintext dashboard_password is set (pre-migration)

    Result is cached; call ``invalidate_auth_required_cache()`` after
    dashboard-setup or migration changes the auth state.
    """
    global _auth_required_cache
    if _auth_required_cache is not None:
        return _auth_required_cache
    settings = get_settings()
    if settings.get("auth_password_hash"):
        _auth_required_cache = True
        return True
    if settings.get("dashboard_password"):
        _auth_required_cache = True
        return True
    if os.environ.get("DASHBOARD_PASSWORD"):
        _auth_required_cache = True
        return True
    _auth_required_cache = False
    return False


def invalidate_auth_required_cache() -> None:
    """Clear the cached auth-required flag so it's re-evaluated."""
    global _auth_required_cache
    _auth_required_cache = None


def validate_api_key(key: str) -> bool:
    """Check an API key against stored SHA-256 hashes in settings.json.

    API keys are stored as a list of {hash, name, prefix, created} dicts.
    The key format is ``pp_`` + 48 hex chars.  We hash the full key with
    SHA-256 and compare against stored hashes.  SHA-256 is sufficient here
    because API keys are high-entropy random tokens (not user passwords).
    """
    if not key or not key.startswith("pp_"):
        return False
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    settings = get_settings()
    api_keys = settings.get("auth_api_keys", [])
    # Constant-time compare (gap-wave-4) — evaluate every key so a match can't be
    # timed. compare_digest over the two hex digests avoids the `==` short-circuit.
    import hmac as _hmac
    matched = False
    for k in api_keys:
        if _hmac.compare_digest(str(k.get("hash", "")), key_hash):
            matched = True
    return matched


def migrate_dashboard_auth() -> None:
    """Hash legacy plaintext dashboard_password to bcrypt if not already migrated.

    Called on startup from both server.py (headless) and dashboard.py (desktop).
    Safe to call multiple times — no-ops if already migrated or no auth configured.
    """
    settings = get_settings()
    if settings.get("auth_password_hash"):
        # Already migrated. _seed_settings_from_env can re-write the legacy
        # plaintext keys back into settings.json on every restart from the
        # DASHBOARD_PASSWORD/USER env vars (Docker compose), so scrub them
        # here too — otherwise the plaintext sits next to the bcrypt hash and
        # defeats the migration. (BUG-004 in 2.14.6.)
        if settings.get("dashboard_password") or settings.get("dashboard_user"):
            delete_settings_keys(["dashboard_password", "dashboard_user"])
        return

    legacy_pw = settings.get("dashboard_password") or os.environ.get("DASHBOARD_PASSWORD", "")
    if not legacy_pw:
        return  # No auth configured

    legacy_user = settings.get("dashboard_user") or os.environ.get("DASHBOARD_USER", "admin")
    hashed = hash_password(legacy_pw)
    save_settings({
        "auth_username": legacy_user,
        "auth_password_hash": hashed,
    })
    # Remove plaintext password from settings (env var remains but is ignored
    # once the hash exists)
    delete_settings_keys(["dashboard_password", "dashboard_user"])
    invalidate_auth_required_cache()
    logger.info("Migrated dashboard password to bcrypt hash for user '%s'", legacy_user)


# Legacy SoFurry login fields, dead since 3.4.0. The suffix match below also
# catches the per-account variants, which are keyed "acct_<N>_<field>"
# (see account_setting_key).
_SF_LEGACY_CRED_KEYS = ("sf_username", "sf_password", "sf_totp_code",
                        "sf_session_cookies")


def migrate_sofurry_credentials() -> None:
    """Scrub the dead SoFurry email/password/cookie credentials (3.4.0).

    SoFurry shipped an official API, so PawPoller authenticates with a Personal
    Access Token and never logs in. The old fields are not merely unused — leaving
    a real password and a session cookie sitting in the vault is a standing
    liability for a credential nothing can spend any more.

    This cannot migrate anything *into* the token: a PAT is minted by the user on
    SoFurry, so a password cannot be exchanged for one. The user has to reconnect
    once. The scrub is therefore unconditional, and deliberately runs even when no
    token is configured yet — the dead credential should not survive on the theory
    that it might be needed, because it cannot be.

    Called on startup from both server.py (headless) and dashboard.py (desktop).
    Safe to call repeatedly — no-ops once the keys are gone.
    """
    settings = get_settings()
    stale = [k for k in settings
             if any(k == base or k.endswith("_" + base) for base in _SF_LEGACY_CRED_KEYS)]
    if not stale:
        return
    delete_settings_keys(stale)
    logger.info(
        "SoFurry: cleared %d legacy login field(s) — the official API uses a "
        "Personal Access Token (reconnect SoFurry in Settings if you have not yet)",
        len(stale),
    )


# ── Run-on-startup ────────────────────────────────────────────
# Per-OS implementation behind a single get/set pair:
#
#   Windows: HKCU\Software\Microsoft\Windows\CurrentVersion\Run value.
#            Per-user, no admin needed.
#   Linux:   ~/.config/autostart/PawPoller.desktop (XDG autostart spec).
#            Per-user, no root needed. Honoured by GNOME, KDE, XFCE,
#            Cinnamon, MATE, LXQt — every major desktop environment.
#   macOS:   Not implemented yet — would use a launch agent plist at
#            ~/Library/LaunchAgents/com.knaughtykat.pawpoller.plist.
#
# The value/exec string differs by mode:
#   Frozen:  the executable path directly (e.g. "C:\...\PawPoller.exe"
#            on Windows, or the AppImage path on Linux)
#   Dev:     python interpreter + main.py path

_STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_NAME = "PawPoller"  # Windows registry value name


def _linux_autostart_path() -> Path:
    """Return the XDG autostart .desktop path for the current user.

    Honours $XDG_CONFIG_HOME if set (rare), else ~/.config/autostart.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "autostart" / "PawPoller.desktop"


def _exec_command_for_autostart() -> str:
    """Build the Exec= / registry value pointing at this PawPoller install.

    Same logic for both OSes — only the quoting style differs and the
    callers handle that.
    """
    if getattr(sys, "frozen", False):
        # Frozen: sys.executable is the bundled binary (PawPoller.exe or
        # the Linux PyInstaller binary inside the AppImage)
        return sys.executable
    # Dev mode: invoke the interpreter against main.py
    script = str(Path(__file__).resolve().parent / "main.py")
    return f'"{sys.executable}" "{script}"'


def get_run_on_startup() -> bool:
    """Check whether the app is registered to start on user login.

    Returns True iff the per-OS registration exists. Path/exec validity
    is NOT checked — a stale registration still returns True so the UI
    can show the toggle state honestly.
    """
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, _STARTUP_REG_NAME)  # Throws if not found
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return False
    if sys.platform.startswith("linux"):
        return _linux_autostart_path().exists()
    # macOS and others: not implemented
    return False


def set_run_on_startup(enabled: bool) -> None:
    """Add or remove the app from per-user startup.

    Windows: writes/deletes the HKCU Run registry value.
    Linux: writes/removes the XDG autostart .desktop file.
    Other platforms: logs a warning and returns.
    """
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    exe_path = _exec_command_for_autostart()
                    winreg.SetValueEx(key, _STARTUP_REG_NAME, 0, winreg.REG_SZ, exe_path)
                    logger.info("Added to Windows startup: %s", exe_path)
                else:
                    try:
                        winreg.DeleteValue(key, _STARTUP_REG_NAME)
                        logger.info("Removed from Windows startup")
                    except FileNotFoundError:
                        pass  # Already absent
        except OSError as e:
            logger.error("Failed to modify startup registry: %s", e)
        return

    if sys.platform.startswith("linux"):
        desktop_path = _linux_autostart_path()
        if enabled:
            exec_cmd = _exec_command_for_autostart()
            desktop_path.parent.mkdir(parents=True, exist_ok=True)
            # Standard XDG autostart .desktop format. X-GNOME-Autostart-enabled
            # is honoured by GNOME but harmless elsewhere; OnlyShowIn omitted
            # so every DE picks it up.
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=PawPoller\n"
                "Comment=Multi-platform story publishing + analytics\n"
                f"Exec={exec_cmd}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
            try:
                desktop_path.write_text(content, encoding="utf-8")
                logger.info("Added to Linux autostart: %s", desktop_path)
            except OSError as e:
                logger.error("Failed to write Linux autostart file %s: %s", desktop_path, e)
        else:
            try:
                desktop_path.unlink()
                logger.info("Removed from Linux autostart: %s", desktop_path)
            except FileNotFoundError:
                pass  # Already absent
            except OSError as e:
                logger.error("Failed to remove Linux autostart file %s: %s", desktop_path, e)
        return

    logger.warning("set_run_on_startup is not supported on this platform (%s)", sys.platform)
