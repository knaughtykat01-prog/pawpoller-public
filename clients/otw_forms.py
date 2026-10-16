"""OTW Archive forms (AO3, SquidgeWorld): pick the form a request posts to.

The archive uses per-form CSRF tokens: each form's ``authenticity_token`` is only valid for that form's own action.
A page carries several (the site-banner dismiss form, the skin switchers, the work form), so "the first token on the
page" is the wrong one whenever a banner is up, and AO3 answers the POST with ``/auth_error`` (4.78.1, AO3AUTHERR).
"""
from __future__ import annotations

import re
from html import unescape

_FORM_RE = re.compile(r'<form\b[^>]*\baction="([^"]*)"[^>]*>.*?</form>', re.S | re.I)
# Site chrome that appears on every page: never the form a caller wants.
_CHROME_RE = re.compile(r"/users/[^/]+/end_banner|/skins/(?:\d+/set|unset)")


def otw_form(page: str, action: str) -> str:
    """The ``<form>`` on *page* whose action path fully matches the regex *action*, e.g. ``r"/works/\\d+"``.

    No match: the page without its chrome forms, so a token search still can't land on the banner's or a skin's.
    """
    for m in _FORM_RE.finditer(page):
        path = unescape(m.group(1)).split("?", 1)[0].split("#", 1)[0]
        path = re.sub(r"^https?://[^/]+", "", path).rstrip("/") or "/"
        if re.fullmatch(action, path):
            return m.group(0)
    return _FORM_RE.sub(lambda m: "" if _CHROME_RE.search(m.group(1)) else m.group(0), page)
