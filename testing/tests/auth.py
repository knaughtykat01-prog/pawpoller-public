"""Dashboard auth tests — bcrypt, TOTP, API key, session, escape, rate limiter.

All read-only. Tests synthesize their own secrets and limiter state;
never touches production auth_password_hash or _auth_failures.
"""

from __future__ import annotations

import hashlib
import html
import secrets
import time

import config
from testing.registry import TestContext, register_test


@register_test(
    test_id="auth.bcrypt_roundtrip",
    name="Bcrypt password hash + verify",
    category="Dashboard Auth",
    description="Hash a test password and verify it matches via config.hash_password / verify_password.",
)
async def t_bcrypt_roundtrip(ctx: TestContext) -> None:
    password = "diagnostic-test-" + secrets.token_hex(8)
    hashed = config.hash_password(password)
    ctx.detail("hash_prefix", hashed[:7])  # $2b$XX
    assert hashed != password, "hash equals plaintext"
    assert config.verify_password(password, hashed), "verify_password rejected matching password"
    assert not config.verify_password(password + "x", hashed), "verify_password accepted wrong password"


@register_test(
    test_id="auth.totp_generate_verify",
    name="TOTP generate + verify",
    category="Dashboard Auth",
    description="Generate a TOTP from a known secret and verify it within the window.",
)
async def t_totp(ctx: TestContext) -> None:
    try:
        import pyotp
    except ImportError:
        raise ctx.skip("pyotp not installed")
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    code = totp.now()
    ctx.detail("code", code)
    assert totp.verify(code, valid_window=1), "TOTP didn't verify its own current code"
    # Wrong code should reject
    bad = "000000" if code != "000000" else "111111"
    assert not totp.verify(bad, valid_window=1), "TOTP accepted obviously-wrong code"


@register_test(
    test_id="auth.api_key_hash",
    name="API key hash round-trip",
    category="Dashboard Auth",
    description="SHA-256 hash a synthetic API key and verify lookup logic.",
)
async def t_api_key(ctx: TestContext) -> None:
    key = "pp_" + secrets.token_hex(24)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    ctx.detail("key_length", len(key))
    ctx.detail("digest_length", len(digest))
    assert len(key) == 51, f"unexpected key length {len(key)}"
    assert len(digest) == 64, "SHA-256 digest wrong length"
    # Re-hash should be deterministic
    again = hashlib.sha256(key.encode("utf-8")).hexdigest()
    assert digest == again, "SHA-256 not deterministic"


@register_test(
    test_id="auth.session_cookie_sign_unsign",
    name="Session cookie sign + unsign",
    category="Dashboard Auth",
    description="itsdangerous TimedSerializer signs and reads back a session payload.",
)
async def t_session(ctx: TestContext) -> None:
    try:
        from itsdangerous import URLSafeTimedSerializer, BadSignature
    except ImportError:
        raise ctx.skip("itsdangerous not installed")
    secret = secrets.token_hex(16)
    s = URLSafeTimedSerializer(secret, salt="diagnostic-test")
    payload = {"u": "diagnostic-user"}
    signed = s.dumps(payload)
    ctx.detail("signed_length", len(signed))
    out = s.loads(signed, max_age=60)
    assert out == payload, f"round-trip mismatch: {out!r}"
    # Tampered token should fail
    tampered = signed[:-2] + ("AA" if signed[-2:] != "AA" else "BB")
    bad_caught = False
    try:
        s.loads(tampered, max_age=60)
    except BadSignature:
        bad_caught = True
    assert bad_caught, "tampered signature was accepted"


@register_test(
    test_id="auth.escape_html",
    name="HTML escape (XSS guard)",
    category="Dashboard Auth",
    description="Standard library html.escape converts dangerous chars.",
)
async def t_escape_html(ctx: TestContext) -> None:
    payload = '<script>alert("xss")</script> "& evil\''
    out = html.escape(payload, quote=True)
    ctx.detail("escaped", out)
    for forbidden in ["<script>", '"', "'", "&"]:
        # `&` must be encoded as &amp;; `<` as &lt; etc.
        if forbidden == "&":
            assert out.count("&amp;") >= 1, "& not encoded"
        elif forbidden == "<script>":
            assert "<script>" not in out, "<script> survived escape"
        elif forbidden == '"':
            assert "&quot;" in out, '" not encoded'
        elif forbidden == "'":
            assert "&#x27;" in out or "&apos;" in out or "&#39;" in out, "' not encoded"


@register_test(
    test_id="auth.rate_limiter_isolated",
    name="Rate limiter triggers after threshold",
    category="Dashboard Auth",
    description="Synthetic in-memory limiter: 11 failures from one fake IP should trip the lockout.",
)
async def t_rate_limiter(ctx: TestContext) -> None:
    # We deliberately do NOT touch dashboard._auth_failures (production
    # state). We reimplement the windowed-counter logic against an
    # isolated dict and assert the threshold behaviour matches the
    # documented contract.
    failures: dict[str, list[float]] = {}
    window = 300
    threshold = 10
    fake_ip = "203.0.113.99"

    def record(ip: str) -> None:
        now = time.monotonic()
        attempts = failures.setdefault(ip, [])
        attempts.append(now)
        cutoff = now - window
        failures[ip] = [t for t in attempts if t > cutoff]

    def limited(ip: str) -> bool:
        attempts = failures.get(ip, [])
        cutoff = time.monotonic() - window
        recent = [t for t in attempts if t > cutoff]
        return len(recent) > threshold

    for i in range(threshold):
        record(fake_ip)
        assert not limited(fake_ip), f"limited after only {i + 1} attempts"
    record(fake_ip)  # the 11th
    assert limited(fake_ip), "should be limited after 11 attempts"
    ctx.detail("attempts_recorded", len(failures[fake_ip]))
