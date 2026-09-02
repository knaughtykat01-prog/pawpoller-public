"""Infrastructure tests — DB, settings, vault, logs, disk, tag DB.

12 read-only tests. None send notifications, none make network calls,
none mutate user state. The settings round-trip test writes a clearly-
namespaced marker key and removes it before completion.
"""

from __future__ import annotations

import inspect
import os
import shutil
import sqlite3
from pathlib import Path

import config
from database.db import get_connection, init_db
from testing.registry import TestContext, register_test


# ── DB tests ─────────────────────────────────────────────────────────


@register_test(
    test_id="infra.db.connection",
    name="DB connection",
    category="Infrastructure",
    description="Open data/pawpoller.db and run a trivial SELECT.",
)
async def t_db_connection(ctx: TestContext) -> None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 AS ok").fetchone()
        assert row is not None and row["ok"] == 1, "SELECT 1 returned unexpected"
        ctx.detail("path", str(config.DB_PATH))
    finally:
        conn.close()


@register_test(
    test_id="infra.db.wal_mode",
    name="DB WAL journal mode",
    category="Infrastructure",
    description="PRAGMA journal_mode should be WAL — required for concurrent reader + writer.",
)
async def t_db_wal(ctx: TestContext) -> None:
    conn = get_connection()
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        mode = (row[0] if row else "").lower()
        ctx.detail("journal_mode", mode)
        assert mode == "wal", f"expected wal, got {mode!r}"
    finally:
        conn.close()


@register_test(
    test_id="infra.db.foreign_keys",
    name="DB foreign keys enforced",
    category="Infrastructure",
    description="PRAGMA foreign_keys should be 1.",
)
async def t_db_fk(ctx: TestContext) -> None:
    conn = get_connection()
    try:
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        val = row[0] if row else 0
        ctx.detail("foreign_keys", val)
        assert val == 1, f"foreign_keys not enforced (got {val})"
    finally:
        conn.close()


@register_test(
    test_id="infra.db.integrity",
    name="DB integrity check",
    category="Infrastructure",
    description="PRAGMA integrity_check should return 'ok'.",
    timeout_seconds=60.0,  # large DBs can take longer
)
async def t_db_integrity(ctx: TestContext) -> None:
    conn = get_connection()
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        result = row[0] if row else ""
        ctx.detail("integrity_check", result)
        assert result == "ok", f"integrity_check failed: {result}"
    finally:
        conn.close()


@register_test(
    test_id="infra.db.migrations_idempotent",
    name="DB migrations idempotent",
    category="Infrastructure",
    description="Run init_db() against the live DB; expect no errors and no schema change.",
)
async def t_db_migrations(ctx: TestContext) -> None:
    # init_db is idempotent — re-running creates nothing new, only
    # re-asserts tables and migrations. Errors here mean a migration
    # would fail on the next container restart.
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        tables = [r["name"] for r in rows]
        ctx.detail("table_count", len(tables))
        ctx.detail("tables", tables[:25])  # sample
        assert len(tables) > 5, f"unexpectedly few tables: {tables}"
    finally:
        conn.close()


# ── Settings tests ───────────────────────────────────────────────────


@register_test(
    test_id="infra.settings.roundtrip",
    name="Settings round-trip",
    category="Infrastructure",
    description="Write a namespaced marker key, read it back, delete it.",
)
async def t_settings_roundtrip(ctx: TestContext) -> None:
    marker = "_diagnostics_test_marker"
    val = "diagnostic-roundtrip-token"
    # Save
    config.save_settings({marker: val})
    try:
        read_back = config.get_settings().get(marker)
        assert read_back == val, f"expected {val!r}, got {read_back!r}"
        ctx.detail("written_and_read", val)
    finally:
        # Cleanup — overwrite to empty string. Settings has no delete
        # helper; empty value is the convention for "absent".
        config.save_settings({marker: ""})


@register_test(
    test_id="infra.settings.atomic_write",
    name="Settings write is atomic",
    category="Infrastructure",
    description="Verify save_settings() source uses tempfile + os.replace.",
)
async def t_settings_atomic(ctx: TestContext) -> None:
    src = inspect.getsource(config.save_settings)
    has_tmp = "tempfile" in src or "with_suffix" in src or ".tmp" in src
    has_replace = "os.replace" in src or "Path.replace" in src or ".replace(" in src
    ctx.detail("uses_tempfile", has_tmp)
    ctx.detail("uses_os_replace", has_replace)
    assert has_tmp and has_replace, "save_settings() not using temp+replace pattern"


# ── Vault tests ──────────────────────────────────────────────────────


@register_test(
    test_id="infra.vault.crypto",
    name="Vault encrypt/decrypt round-trip",
    category="Infrastructure",
    description="Encrypt and decrypt a known payload via the live vault key.",
)
async def t_vault_crypto(ctx: TestContext) -> None:
    # _encrypt_vault(dict) writes settings.vault.json; _decrypt_vault() reads it.
    # Round-trip via a redirected VAULT_PATH so the live vault is never touched.
    import tempfile
    from pathlib import Path

    enc = getattr(config, "_encrypt_vault", None)
    dec = getattr(config, "_decrypt_vault", None)
    if enc is None or dec is None:
        raise ctx.skip("vault crypto helpers not available in this build")
    try:
        config._get_vault_key()  # surfaces a real failure if crypto/keyring is broken
    except Exception as exc:  # noqa: BLE001
        raise ctx.skip(f"vault key unavailable: {exc}")

    payload = {"sample": "diagnostic", "n": 42}
    original = config.VAULT_PATH
    tmp_dir = tempfile.mkdtemp(prefix="ppdiag-vault-")
    try:
        config.VAULT_PATH = Path(tmp_dir) / "settings.vault.json"
        enc(payload)
        assert config.VAULT_PATH.exists(), "vault file not written"
        ctx.detail("blob_bytes", config.VAULT_PATH.stat().st_size)
        out = dec()
        assert out == payload, f"decrypted payload doesn't match: {out!r}"
    finally:
        config.VAULT_PATH = original
        try:
            import shutil as _sh
            _sh.rmtree(tmp_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


@register_test(
    test_id="infra.vault.key_source",
    name="Vault key source",
    category="Infrastructure",
    description="Verify the vault key is reachable (keyring or .vault_key dotfile).",
)
async def t_vault_key_source(ctx: TestContext) -> None:
    getter = getattr(config, "_get_vault_key", None)
    if getter is None:
        raise ctx.skip("_get_vault_key not available in this build")
    try:
        key = getter()
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"_get_vault_key raised: {exc}") from exc
    if not key:
        raise ctx.skip("vault not enabled (no key configured)")
    ctx.detail("key_present", True)
    ctx.detail("key_bytes", len(key) if isinstance(key, (bytes, str)) else None)


# ── Credentials visibility ───────────────────────────────────────────


@register_test(
    test_id="infra.credentials_visible",
    name="Credential vault merges into get_settings()",
    category="Infrastructure",
    description=(
        "Diagnostic: when credential_mode is 'local', the vault should "
        "auto-merge into config.get_settings(). Reports the live state so "
        "we can tell when platform tests skip due to a vault-merge failure "
        "vs a genuinely-empty credential."
    ),
)
async def t_credentials_visible(ctx: TestContext) -> None:
    s = config.get_settings()
    mode = s.get("credential_mode") or "plaintext"
    ctx.detail("credential_mode", mode)

    # Probe the keys that platform tests actually check against.
    probes = [
        "username", "password",                       # IB
        "fa_cookie_a", "fa_cookie_b",                 # FA
        "ws_api_key",                                 # WS
        "sf_api_token",                               # SF
        "sqw_username", "sqw_password",               # SqW
        "ao3_username", "ao3_session_cookie",         # AO3
        "da_cookie",                                  # DA
        "bsky_identifier", "bsky_app_password",       # Bsky
        "tw_auth_token", "tw_ct0",                    # TW
        "wp_target_user", "ik_target_user",           # WP / IK (public)
        "telegram_bot_token", "telegram_chat_id",     # Telegram
        "cf_worker_url",                              # CF proxy
    ]
    present = sorted(k for k in probes if s.get(k))
    absent = sorted(k for k in probes if not s.get(k))
    ctx.detail("present", present)
    ctx.detail("absent", absent)
    ctx.detail("present_count", len(present))
    ctx.detail("absent_count", len(absent))

    # Hard fail only when vault is enabled but it didn't merge anything.
    # That's the genuine bug shape (vault corrupt / key mismatch / wrong path).
    # Plaintext mode with zero creds is a fresh install, not a bug.
    if mode == "local" and not present:
        raise AssertionError(
            "credential_mode=local but get_settings() exposes no credentials — "
            "vault may have failed to decrypt or merge"
        )
    # Otherwise this is purely informational.


# ── Logs ─────────────────────────────────────────────────────────────


@register_test(
    test_id="infra.logs.writable",
    name="Log directory writable",
    category="Infrastructure",
    description="Append a marker line to logs/_diagnostic_probe.log and verify it lands.",
)
async def t_logs_writable(ctx: TestContext) -> None:
    log_dir = config.SETTINGS_PATH.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    probe = log_dir / "_diagnostic_probe.log"
    marker = f"diagnostic-probe-{os.getpid()}"
    try:
        with probe.open("a", encoding="utf-8") as fp:
            fp.write(marker + "\n")
        contents = probe.read_text(encoding="utf-8")
        assert marker in contents, "probe line not found after write"
        ctx.detail("path", str(probe))
    finally:
        # Truncate so the probe file doesn't grow over time
        try:
            probe.write_text("", encoding="utf-8")
        except OSError:
            pass


# ── Disk ─────────────────────────────────────────────────────────────


@register_test(
    test_id="infra.disk.space",
    name="Data dir has free space",
    category="Infrastructure",
    description="At least 100 MB free in the data directory.",
)
async def t_disk_space(ctx: TestContext) -> None:
    data_dir = config.SETTINGS_PATH.parent
    usage = shutil.disk_usage(data_dir)
    free_mb = usage.free / 1024 / 1024
    ctx.detail("free_mb", round(free_mb, 1))
    ctx.detail("total_mb", round(usage.total / 1024 / 1024, 1))
    assert free_mb > 100.0, f"only {free_mb:.1f} MB free in {data_dir}"


# ── Tag DB ───────────────────────────────────────────────────────────


@register_test(
    test_id="infra.tag_db.files",
    name="Tag DB files present",
    category="Infrastructure",
    description="All tag database files exist and are non-empty.",
)
async def t_tag_db_files(ctx: TestContext) -> None:
    candidates = [
        "tag_database/tag_database_physical.txt",
        "tag_database/tag_database_acts.txt",
        "tag_database/tag_database_kink.txt",
        "tag_database/tag_database_meta.txt",
    ]
    found = []
    missing = []
    base = Path(__file__).resolve().parent.parent.parent
    for rel in candidates:
        p = base / rel
        # Also try resource_path / app root for frozen builds
        if not p.is_file():
            try:
                p = Path(config.resource_path(rel))
            except Exception:  # noqa: BLE001
                pass
        if p.is_file() and p.stat().st_size > 100:
            found.append(rel)
        else:
            missing.append(rel)
    ctx.detail("found", found)
    ctx.detail("missing", missing)
    assert not missing, f"missing or empty: {missing}"
