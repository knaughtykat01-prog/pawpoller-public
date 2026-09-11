"""Every platform in the registry has a Getting Started guide (4.28.1).

The hub renders a card per `window.PLATFORMS` entry that has a guide and silently
skips the rest, so a platform shipped without one simply never appears on the page
— which is how six of them (fn, fbr, pod, sc, ng, yt) were missing for a while.
"""
from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "frontend" / "js"
PLATFORMS = (JS / "platforms.js").read_text(encoding="utf-8")
GUIDES = (JS / "platform_guides.js").read_text(encoding="utf-8")


def _registry_codes() -> list[str]:
    return re.findall(r"\{ code: '(\w+)',\s+label:", PLATFORMS)


def _guide_codes() -> list[str]:
    block = GUIDES[GUIDES.index("const GUIDES = {"):GUIDES.index("function _plat(code)")]
    return re.findall(r"^    (\w+): \{", block, re.M)


def test_every_registered_platform_has_a_guide():
    registry = _registry_codes()
    assert len(registry) >= 24
    guides = set(_guide_codes())
    missing = [c for c in registry if c not in guides]
    assert not missing, f"platforms with no Getting Started guide: {missing}"


def test_every_guide_names_where_the_credential_goes():
    for code in _guide_codes():
        start = GUIDES.index(f"\n    {code}: {{")
        entry = GUIDES[start:GUIDES.index("\n    },", start)]
        assert "paste:" in entry and "renew:" in entry and "steps:" in entry, code
