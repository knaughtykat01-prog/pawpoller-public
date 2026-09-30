"""Colour contrast, WCAG 2.2 AA (spec 012): every app theme and the marketing site's palette.

Pure arithmetic over the colour tokens — no browser. Text needs 4.5:1 against each background it
sits on; accent, status colours and the focus ring (which uses --accent) need 3:1. A failure names
the surface, the pair and the ratio, so a new theme or a new shade can't arrive failing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOKENS = ROOT / "frontend" / "css" / "tokens.css"


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_ratio_matches_the_standards_examples():
    assert round(ratio("#000000", "#ffffff"), 1) == 21.0
    assert round(ratio("#777777", "#ffffff"), 2) == 4.48          # the classic "just fails"


# ── App themes ───────────────────────────────────────────────────────────────

BACKGROUNDS = ("--bg-primary", "--bg-secondary", "--bg-card", "--bg-tertiary", "--bg-hover")   # hover = buttons
TEXT = ("--text-primary", "--text-secondary", "--text-muted")               # 4.5:1
UI = ("--accent", "--success", "--warning", "--danger", "--info")          # 3:1 (focus ring = --accent)


def _themes() -> dict[str, dict[str, str]]:
    css = TOKENS.read_text(encoding="utf-8")
    blocks: dict[str, dict[str, str]] = {}
    for m in re.finditer(r'(:root|\[data-theme="([a-z0-9_]+)"\])\s*\{(.*?)\n\}', css, re.S):
        name = m.group(2) or "default"
        found = dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{3,6})\b", m.group(3)))
        blocks.setdefault(name, {}).update(found)
    base = blocks["default"]
    return {name: {**base, **v} for name, v in blocks.items()}


def test_every_app_theme_meets_aa():
    themes = _themes()
    assert "default" in themes and "high_contrast" in themes and len(themes) >= 10, sorted(themes)
    fails = []
    for theme, t in themes.items():
        for fg, need in [(f, 4.5) for f in TEXT] + [(f, 3.0) for f in UI]:
            for bg in BACKGROUNDS:
                r = ratio(t[fg], t[bg])
                if r < need:
                    fails.append(f"app:{theme} {fg}/{bg} {r:.2f}<{need}")
    assert not fails, "\n".join(fails)


# ── Marketing site ───────────────────────────────────────────────────────────

SITE = ROOT / "site"
SITE_BACKGROUNDS = ("ink-800", "ink-900", "ink-950")
# Text shades that sit on a light surface, not the dark page — checked against that surface instead.
ON_LIGHT = {"ink-600": "#ffffff",            # monograms on the white platform-logo tiles
            "ink-950": "copper-500"}         # the primary button's label on copper


def _palette() -> dict[str, str]:
    cfg = (SITE / "tailwind.config.mjs").read_text(encoding="utf-8")
    colours = cfg[cfg.index("colors:"):cfg.index("fontFamily")]
    out = {}
    for fam, body in re.findall(r"(\w+):\s*\{([^{}]*)\}", colours):       # innermost blocks only
        for shade, hexv in re.findall(r"(\d+):\s*'(#[0-9a-fA-F]{6})'", body):
            out[f"{fam}-{shade}"] = hexv
    return out


@pytest.mark.skipif(not SITE.is_dir(), reason="the marketing site isn't in this copy")
def test_every_site_text_colour_meets_aa():
    pal = _palette()
    used = set()
    for p in (SITE / "src").rglob("*"):
        if p.suffix in (".astro", ".css", ".ts", ".mjs"):
            used |= set(re.findall(r"text-((?:ink|copper|sage)-\d+)\b(?!/)", p.read_text(encoding="utf-8")))
    assert used, "found no text colours — the scan no longer matches the site"
    undefined = sorted(u for u in used if u not in pal)
    assert not undefined, f"text shades used but not defined in the palette (they silently inherit): {undefined}"
    fails = []
    for shade in sorted(used):
        bgs = [ON_LIGHT[shade]] if shade in ON_LIGHT else SITE_BACKGROUNDS
        for bg in bgs:
            r = ratio(pal[shade], pal.get(bg, bg))
            if r < 4.5:
                fails.append(f"site {shade}/{bg} {r:.2f}<4.5")
    assert not fails, "\n".join(fails)
