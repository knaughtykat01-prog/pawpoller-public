"""Settings pages behind the left rail (4.25.0, SETTINGSNAV phase 1) — source-level pins.

The render still builds the old tab panels and moves their blocks into pages, so the
two registries in app.js are what keep every block reachable: every old tab key must
map to a page, every block's own `data-page` must name a page, and every page must be
on the rail. A block that names a page nobody renders would silently vanish.
"""
from __future__ import annotations

import re
from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js").read_text(encoding="utf-8")


def _pages() -> list[str]:
    block = APP_JS[APP_JS.index("SETTINGS_PAGES: ["):APP_JS.index("SETTINGS_TAB_PAGE: {")]
    return re.findall(r"\{ key: '(\w+)'", block)


def _tab_map() -> dict[str, str]:
    start = APP_JS.index("SETTINGS_TAB_PAGE: {")
    block = APP_JS[start:APP_JS.index("}", start)]
    return dict(re.findall(r"(\w+): '(\w+)'", block))


def test_every_old_tab_and_every_tagged_block_lands_on_a_rail_page():
    pages = _pages()
    assert len(pages) == 11 and len(set(pages)) == 11
    tab_map = _tab_map()
    old_tabs = set(re.findall(r'data-tab-content="(\w+)"', APP_JS))
    assert old_tabs, "the old panels are still what the template renders"
    missing = old_tabs - set(tab_map)
    assert not missing, f"old tabs with no page: {missing}"
    assert set(tab_map.values()) <= set(pages)
    tagged = set(re.findall(r'data-page="(\w+)"', APP_JS))
    assert tagged <= set(pages), f"blocks tagged for a page nobody renders: {tagged - set(pages)}"
    # the blocks that left General must have somewhere to go
    for must in ("connection", "preferences", "polling", "notifications", "publishing", "about"):
        assert must in tagged, must


def test_the_rail_keeps_the_search_and_the_page_head():
    assert 'id="settings-rail"' in APP_JS and 'id="settings-search"' in APP_JS
    assert "settings-nav-item" in APP_JS and "settings-page-head" in APP_JS
    # the poll actions moved off the header onto the Polling page; the header Save is gone (4.27.0)
    head = APP_JS[APP_JS.index("async renderSettings()"):APP_JS.index('id="settings-rail"')]
    assert 'id="poll-now-btn"' not in head and 'save-all-settings-btn' not in APP_JS
    assert 'id="poll-now-btn"' in APP_JS
    # the tour points at the rail, not the retired tab strip
    tour = (Path(__file__).resolve().parent.parent / "frontend" / "js" / "tour.js").read_text(encoding="utf-8")
    assert "#settings-tabs" not in tour and "data-stab" not in tour


def test_platform_accordions_are_siblings_not_nested():
    """4.26.0: the list column is built from the panel's top-level accordions, so a
    platform card rendered inside another card's body (as sc/ng/yt were inside
    FurryNetwork's from 4.22.0 to 4.25.0) never gets a row. Every accordion open
    must be balanced before the next one starts."""
    opens = [m.start() for m in re.finditer(r'<details class="settings-accordion" data-platform="(\w+)">', APP_JS)]
    assert len(opens) >= 22
    for a, b in zip(opens, opens[1:]):
        chunk = APP_JS[a:b]
        assert chunk.count("<details") == chunk.count("</details>"), chunk[:80]


def test_platforms_list_detail_is_wired():
    assert "_buildPlatformsListDetail(pane, platforms)" in APP_JS
    assert "pset-list" in APP_JS and "pset-detail" in APP_JS and "#/settings/platforms/" in APP_JS
    assert "_focusPlatformFromHash" not in APP_JS       # replaced by the row selection


def test_notification_matrix_replaces_the_per_site_listeners():
    """4.27.0: one delegated listener in the matrix saves every site switch; the
    twenty-one copied blocks are gone (YouTube's had been wired to SoundCloud's
    toggle). Every rendered switch must still have a preference key the matrix
    derives from its id."""
    assert "_buildNotificationMatrix()" in APP_JS and "NotifToggle" not in APP_JS
    ids = set(re.findall(r'id="pref-([a-z0-9]+)-notifications"', APP_JS))
    assert len(ids) >= 21
    for code in ids:
        assert f"{code}_notifications_enabled" in APP_JS, code
    assert 'id="pref-tw-save-tokens"' in APP_JS and "tw_roundrobin_save_tokens: e.target.checked" in APP_JS

