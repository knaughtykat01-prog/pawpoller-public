"""Spec 087 in a real browser: "Needs rating" on a piece's page, and Patreon rows in the found list.

The details form shows General selected for a piece with no rating and sends it on every save, so a marked piece
must have no rating box at all: its save sends no rating, and only "Rate this piece" rates it. Text from Patreon
(a post's title, a tier's name) is drawn as text.
"""
from __future__ import annotations

import pytest

from tests.test_frontend_runs import _offline, _tooling_missing, _Watched, app_url, browser  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(bool(_tooling_missing()), reason=_tooling_missing() or "ok")

PIECE = """async (marked) => {
    const m = { name: 'Sample_Piece', title: 'Sample Piece', description: '', rating: '', characters: [], tags: [],
                needs_rating: marked, pat_paid: marked, pat_pick_needed: marked, pat_pick_note: '<b>note</b>' };
    const host = document.createElement('div');
    host.innerHTML = Masterpieces._canonicalHtml(m.name, m) + '<textarea id="mp-e-tags">fox, sample</textarea>'
        + Masterpieces._publishHtml(m);
    document.body.appendChild(host);
    const sent = Masterpieces._readCanonical();
    const out = { hasRatingBox: !!host.querySelector('#mp-e-rating'), rateButtons: [...host.querySelectorAll('[data-mp-rate]')].map(b => b.dataset.mpRate),
                  sendsRating: 'rating' in sent, tags: sent.tags, text: host.innerText, boldFromNote: host.querySelectorAll('p b').length };
    // "Rate this piece" calls the rate route and nothing else.
    const calls = [];
    API.rateMasterpieces = async (names, rating) => { calls.push([names, rating]); return { rated: names, failed: [] }; };
    API.patchMasterpiece = async () => { calls.push('PATCH'); return {}; };
    Masterpieces._toast = () => {};
    if (marked) await Masterpieces._rate(['Sample_Piece'], 'adult');
    out.calls = calls;
    host.remove();
    return out;
}"""

FOUND = """() => {
    const host = document.createElement('div');
    host.innerHTML = Submissions._discRow({ platform: 'pat', submission_id: '801', title: '<img src=x onerror="window.__hit=1">',
        audience_label: '<b>Sketch</b> club', audience: 'tiers', has_pictures: true, page_18: true, kind: 'art',
        thumbnail_url: '', url: 'https://www.patreon.com/posts/801' }, 0);
    document.body.appendChild(host);
    const out = { imgs: host.querySelectorAll('img').length, bolds: host.querySelectorAll('b').length, text: host.innerText,
                  canImport: Submissions._canImportArt({ platform: 'pat', has_pictures: true, thumbnail_url: '' }),
                  textPost: Submissions._canImportArt({ platform: 'pat', has_pictures: false, thumbnail_url: '' }),
                  importBtn: !!host.querySelector('#disc-import-btn-0'), hit: !!window.__hit };
    host.remove();
    return out;
}"""


def test_a_marked_piece_has_no_rating_box_and_its_save_sends_no_rating(browser, app_url):  # noqa: F811
    tab = _Watched(browser, app_url, {"width": 1280, "height": 900})
    try:
        tab.open("#/library")
        marked = tab.page.evaluate(PIECE, True)
        assert not marked["hasRatingBox"] and marked["rateButtons"] == ["general", "mature", "adult"]
        assert not marked["sendsRating"], "editing tags or the title on a marked piece sends no rating"
        assert marked["tags"] == ["fox", "sample"]
        assert "Needs rating" in marked["text"] and "Was a paid post on Patreon" in marked["text"]
        assert "Pick who sees this on Patreon" in marked["text"] and "<b>note</b>" in marked["text"]
        assert "This was a paid post on Patreon" in marked["text"], "said where one piece is published or scheduled"
        assert marked["boldFromNote"] == 0, "the note is text, not markup"
        assert marked["calls"] == [[["Sample_Piece"], "adult"]], "Rate this piece calls the rate route only"
        plain = tab.page.evaluate(PIECE, False)
        assert plain["hasRatingBox"] and plain["sendsRating"] and plain["rateButtons"] == []
        assert "Needs rating" not in plain["text"] and "paid post" not in plain["text"]
    finally:
        tab.close()


def test_hostile_title_and_tier_name_show_as_text_in_the_found_list(browser, app_url):  # noqa: F811
    tab = _Watched(browser, app_url, {"width": 390, "height": 844})
    try:
        tab.open("#/library")
        r = tab.page.evaluate(FOUND)
        assert r["imgs"] == 0 and r["bolds"] == 0 and not r["hit"]
        assert "<img src=x" in r["text"] and "for <b>Sketch</b> club" in r["text"]
        assert "This page is marked 18+: rate what you import." in r["text"]
        assert r["canImport"] and r["importBtn"] and not r["textPost"]
    finally:
        tab.close()
