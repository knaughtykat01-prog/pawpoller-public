"""Post forms list my sites first (057, POSTSITESONLY).

One shared picker (Components.personaPicker) places the rows for five surfaces, so it is tested once,
in a real browser, over stand-in rows and stand-in accounts: the API calls are replaced in the page.
"""
from __future__ import annotations

import pytest  # noqa: E402

from tests.test_frontend_runs import _offline, _tooling_missing, _Watched, app_url, browser  # noqa: F401  (fixtures)

# The release image has no Playwright: skip there, like test_frontend_runs.
pytestmark = pytest.mark.skipif(bool(_tooling_missing()), reason=_tooling_missing() or "ok")

CODES = ['ib', 'fa', 'sf', 'bsky', 'tw', 'ik', 'ws', 'da', 'e621', 'fbr', 'ig', 'fn', 'tg', 'pod', 'sc', 'ng',
         'yt', 'fb', 'tum', 'thr']

# Accounts on 4 sites, plus a Never-post one and a disabled one (both count as none), persona 1 on two.
ACCOUNTS = [
    {"account_id": 1, "platform": "ib", "enabled": 1, "persona_id": 1, "is_default": 1, "label": "A"},
    {"account_id": 2, "platform": "fa", "enabled": 1, "persona_id": 1, "is_default": 1, "label": "B"},
    {"account_id": 3, "platform": "bsky", "enabled": 1, "persona_id": None, "is_default": 1, "label": "C"},
    {"account_id": 4, "platform": "tg", "enabled": 1, "persona_id": None, "is_default": 1, "label": "D"},
    {"account_id": 5, "platform": "da", "enabled": 1, "never_post": 1, "persona_id": 1, "label": "E"},
    {"account_id": 6, "platform": "ik", "enabled": 0, "persona_id": 1, "label": "F"},
]

RUN = """async ([codes, accounts, hidden, pick, ticked]) => {
    window.HIDDEN_PLATFORMS = hidden;
    API.getPersonas = async () => ({ personas: [{ persona_id: 1, name: 'Inkwolf' }] });
    API.getAccounts = async () => ({ accounts });
    const box = document.createElement('div');
    box.innerHTML = '<div class="pk"></div><div class="rows">' + codes.map(c =>
        `<label class="r" data-platform="${c}"><input type="checkbox" ${ticked.includes(c) ? 'checked' : ''}>` +
        `<span class="slot" data-platform="${c}"></span></label>`).join('') + '</div>';
    document.body.appendChild(box);
    const host = box.querySelector('.pk');
    try { localStorage.setItem('pp-test-picker', pick); } catch (e) {}
    await Components.personaPicker({ host, platforms: codes, selectClass: 'x', storageKey: 'pp-test-picker',
        slot: c => box.querySelector(`.slot[data-platform="${c}"]`),
        row: c => box.querySelector(`.r[data-platform="${c}"]`) });
    const read = () => {
        const rows = [...box.querySelectorAll('.r')].filter(r => !r.hidden);
        const inGroup = rows.filter(r => r.closest('.other-sites')).map(r => r.dataset.platform);
        return { top: rows.filter(r => !r.closest('.other-sites')).map(r => r.dataset.platform), inGroup,
                 tickable: rows.filter(r => !r.querySelector('input').disabled).map(r => r.dataset.platform),
                 summary: (box.querySelector('.other-sites summary') || {}).textContent || '',
                 lead: (box.querySelector('.other-sites-lead') || {}).textContent || '',
                 connect: box.querySelectorAll('.other-sites .other-sites-connect').length,
                 hidden: [...box.querySelectorAll('.r')].filter(r => r.hidden).map(r => r.dataset.platform) };
    };
    const first = read();
    const other = host.querySelector(pick === 'all' ? '[data-persona="1"]' : '[data-persona="all"]');
    if (other) other.click();
    const second = read();
    box.remove();
    return { first, second };
}"""


def _run(tab, hidden=(), pick="all", ticked=(), accounts=ACCOUNTS):
    return tab.page.evaluate(RUN, [CODES, accounts, list(hidden), pick, list(ticked)])


def test_the_picker_places_rows(browser, app_url):  # noqa: F811
    tab = _Watched(browser, app_url, {"width": 390, "height": 844})
    try:
        # All accounts: the 4 sites with a usable account on top, 16 folded; Never-post and disabled count as none.
        r = _run(tab, pick="all")
        a, p = r["first"], r["second"]
        assert sorted(a["top"]) == ["bsky", "fa", "ib", "tg"]
        assert len(a["inGroup"]) == 16 and "da" in a["inGroup"] and "ik" in a["inGroup"]
        assert a["summary"] == "Other sites (16)" and a["connect"] == 16
        assert sorted(a["tickable"]) == sorted(a["top"])        # nothing without an account is tickable
        # Persona mode moves rows both ways: Inkwolf only posts to ib and fa.
        assert sorted(p["top"]) == ["fa", "ib"] and "bsky" in p["inGroup"]
        assert sorted(p["tickable"]) == ["fa", "ib"]

        # And back from the persona to All accounts.
        r = _run(tab, pick="1")
        assert sorted(r["first"]["top"]) == ["fa", "ib"]
        assert sorted(r["second"]["top"]) == ["bsky", "fa", "ib", "tg"]

        # A site switched off isn't offered at all.
        r = _run(tab, hidden=["tg", "yt"], pick="all")
        assert set(r["first"]["hidden"]) == {"tg", "yt"}
        assert "tg" not in r["first"]["top"] + r["first"]["inGroup"]

        # A ticked site that loses its account says so.
        r = _run(tab, pick="all", ticked=["bsky"])
        assert "Bluesky was unticked: no account to post as" in r["second"]["lead"]

        # No account at all: the empty state, with a way to connect one.
        r = _run(tab, pick="all", accounts=[])
        assert r["first"]["top"] == [] and "You haven't connected a site yet" in r["first"]["lead"]
        assert not tab.errors, tab.errors
    finally:
        tab.close()
