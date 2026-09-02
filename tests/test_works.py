"""Unit tests for the unified Submissions hub logic (routes.submissions_api).

`assemble_works` merges stories + artwork into per-work entries with their
posted platforms and persona(s). It's a pure function over already-fetched
data, so these tests pass fixtures directly — no DB or on-disk archives.
"""
from routes.submissions_api import assemble_works, build_discovered, classify_kind

PUBS = [
    # My_Story: posted to ib + sf (account 1); one queued (not posted) on fa.
    {"content_type": "story", "story_name": "My_Story", "platform": "ib", "account_id": 1, "status": "posted"},
    {"content_type": "story", "story_name": "My_Story", "platform": "sf", "account_id": 1, "status": "posted"},
    {"content_type": "story", "story_name": "My_Story", "platform": "fa", "account_id": 1, "status": "queued"},
    # My_Art: posted to fa (account 2).
    {"content_type": "artwork", "story_name": "My_Art", "platform": "fa", "account_id": 2, "status": "posted"},
]
ACCT_TO_PERSONA = {1: 10, 2: 20}
PERSONAS = {
    10: {"persona_id": 10, "name": "Main", "color": "#fff"},
    20: {"persona_id": 20, "name": "Alt", "color": "#000"},
}
STORIES = [
    {"name": "My_Story", "title": "My Story", "rating": "explicit",
     "images": {"cover": "cover.png"}, "word_count": 1200, "chapters": 3,
     "description": "A blurb.", "category": "Adventure", "warnings": ["Violence"]},
]
ARTWORKS = [
    {"name": "My_Art", "title": "My Art", "rating": "mature",
     "image": "img.png", "created_at": "2026-01-01"},
]


def _run(**kw):
    return assemble_works(
        stories=STORIES, artworks=ARTWORKS, pubs=PUBS,
        acct_to_persona=ACCT_TO_PERSONA, personas=PERSONAS, **kw,
    )


def test_lists_both_types_grouped_per_work():
    res = _run(type="all")
    works = {w["name"]: w for w in res["works"]}
    assert set(works) == {"My_Story", "My_Art"}
    # Platforms come ONLY from posted publications, grouped per work.
    assert works["My_Story"]["platforms"] == ["ib", "sf"]
    assert works["My_Story"]["publication_count"] == 3
    assert works["My_Art"]["platforms"] == ["fa"]
    # Personas resolved via account_id -> persona.
    assert works["My_Story"]["persona_names"] == ["Main"]
    assert works["My_Art"]["persona_names"] == ["Alt"]
    # The personas list is returned for the filter UI.
    assert {p["name"] for p in res["personas"]} == {"Main", "Alt"}


def test_type_filter():
    assert [w["content_type"] for w in _run(type="story")["works"]] == ["story"]
    assert [w["content_type"] for w in _run(type="artwork")["works"]] == ["artwork"]


# ── Pooled performance stats + metric sorts (2.147.0) ─────────────

_STAT_STORIES = [{"name": "Quiet", "title": "Quiet"}, {"name": "Hit", "title": "Hit"}]
_STAT_PUBS = [
    # Quiet: two platforms → views pool to 15, faves to 3, comments to 3.
    {"content_type": "story", "story_name": "Quiet", "platform": "fa", "status": "posted",
     "stats": {"views": 10, "favorites_count": 1, "comments_count": 0}},
    {"content_type": "story", "story_name": "Quiet", "platform": "ws", "status": "posted",
     "stats": {"views": 5, "favorites_count": 2, "comments_count": 3}},
    # Hit: AO3. The SITE says hits/kudos, but ao3_queries stores plain
    # views/favorites_count — this fixture used to claim `reads`/`kudos`, which
    # is the same false belief that made posting_queries ask for a `hits` column
    # that doesn't exist and silently drop every AO3 publication's stats.
    {"content_type": "story", "story_name": "Hit", "platform": "ao3", "status": "posted",
     "stats": {"views": 100, "favorites_count": 7, "comments_count": 1}},
    # Wattpad genuinely does use different columns (reads/votes) — the registry
    # resolves those to the canonical views/faves.
    {"content_type": "story", "story_name": "Hit", "platform": "wp", "status": "posted",
     "stats": {"reads": 20, "votes": 3, "comments_count": 0}},
]


def _run_stats(**kw):
    return assemble_works(stories=_STAT_STORIES, artworks=[], pubs=_STAT_PUBS,
                          acct_to_persona={}, personas={}, **kw)["works"]


def test_pools_stats_across_platforms_and_naming_variants():
    works = {w["name"]: w for w in _run_stats()}
    assert works["Quiet"]["stats"] == {
        "views": 15, "favorites": 3, "comments": 3, "score": 0}
    # AO3 views + Wattpad reads pool into views; favorites_count + votes into favourites.
    assert works["Hit"]["stats"] == {
        "views": 120, "favorites": 10, "comments": 1, "score": 0}


def test_sort_by_each_metric():
    assert [w["name"] for w in _run_stats(sort="views")] == ["Hit", "Quiet"]
    assert [w["name"] for w in _run_stats(sort="favorites")] == ["Hit", "Quiet"]
    assert [w["name"] for w in _run_stats(sort="comments")] == ["Quiet", "Hit"]


def test_score_platforms_pool_separately_from_views():
    """e621/Furbooru report a net up−down score, not views. It must never be
    added to a view total (it can be negative), but favourites still pool."""
    pubs = [
        {"content_type": "artwork", "story_name": "Art", "platform": "fa", "status": "posted",
         "stats": {"views": 50, "favorites_count": 2, "comments_count": 0}},
        {"content_type": "artwork", "story_name": "Art", "platform": "e621", "status": "posted",
         "stats": {"score": 63, "favorites_count": 161, "comments_count": 0}},
        {"content_type": "artwork", "story_name": "Art", "platform": "fbr", "status": "posted",
         "stats": {"score": -1, "favorites_count": 2, "comments_count": 1}},
    ]
    works = assemble_works(stories=[], artworks=[{"name": "Art", "title": "Art"}],
                           pubs=pubs, acct_to_persona={}, personas={})["works"]
    assert works[0]["stats"] == {
        "views": 50, "favorites": 165, "comments": 1, "score": 62}


def test_stats_default_to_zero_without_stat_carrying_pubs():
    # PUBS (the shared fixture) carry no `stats` — pooling must not explode.
    works = {w["name"]: w for w in _run(type="all")["works"]}
    assert works["My_Story"]["stats"] == {
        "views": 0, "favorites": 0, "comments": 0, "score": 0}


def test_persona_filter():
    assert [w["name"] for w in _run(type="all", persona=20)["works"]] == ["My_Art"]


def test_search_filter():
    assert [w["name"] for w in _run(type="all", search="story")["works"]] == ["My_Story"]


# ── 2.155.0: fields the merged works hub needs ────────────────────
# The Stories hub (#/posting) folded into the Library, whose cards read from
# /api/works. Its cards showed a blurb, a category chip and a ⚠ warnings
# tooltip — none of which assemble_works used to project, so merging the hubs
# would have silently dropped them.

def test_projects_story_blurb_category_and_warnings():
    works = {w["name"]: w for w in _run(type="all")["works"]}
    assert works["My_Story"]["description"] == "A blurb."
    assert works["My_Story"]["category"] == "Adventure"
    assert works["My_Story"]["warnings"] == ["Violence"]


def test_story_card_fields_default_when_absent():
    # Most stories carry none of these; the card must still render.
    res = assemble_works(stories=[{"name": "Bare", "title": "Bare"}], artworks=[],
                         pubs=[], acct_to_persona={}, personas={}, type="all")
    w = res["works"][0]
    assert (w["description"], w["category"], w["warnings"]) == ("", "", [])


def test_thumb_and_detail_routes():
    works = {w["name"]: w for w in _run(type="all")["works"]}
    assert works["My_Story"]["thumb_url"].startswith("/api/posting/image?story=My_Story")
    assert works["My_Story"]["detail_route"] == "#/posting/story/My_Story"
    assert works["My_Art"]["thumb_url"].startswith("/api/artwork/image?name=My_Art")
    assert works["My_Art"]["detail_route"] == "#/artwork/image/My_Art"


# ── Phase 2: discovered (unlinked) bucket ──────────────────────────

CFG_FA = {"id_col": "submission_id", "title_col": "title",
          "url_template": "https://www.furaffinity.net/view/{id}/"}
CFG_SF = {"id_col": "submission_id", "title_col": "title",
          "url_template": "https://sofurry.com/s/{id}"}


def test_discovered_excludes_linked_and_normalizes():
    rows_fa = [
        {"submission_id": 111, "title": "Linked Art", "category": "Artwork (Digital)",
         "thumbnail_url": "http://t/1.jpg", "posted_at": "2026-02-02"},
        {"submission_id": 222, "title": "Unlinked Art", "category": "Artwork (Digital)",
         "thumbnail_url": "http://t/2.jpg", "posted_at": "2026-03-03"},
    ]
    rows_sf = [
        {"submission_id": 333, "title": "Unlinked Story", "content_type": "Writing",
         "posted_at": "2026-01-01"},
    ]
    linked = {("fa", "111")}  # 111 already has a publication
    out = build_discovered([("fa", CFG_FA, rows_fa), ("sf", CFG_SF, rows_sf)], linked)

    assert {(d["platform"], d["submission_id"]) for d in out} == {("fa", "222"), ("sf", "333")}
    by = {d["submission_id"]: d for d in out}
    assert by["222"]["type"] == "Artwork (Digital)"          # type from `category`
    assert by["222"]["thumbnail_url"] == "http://t/2.jpg"
    assert by["222"]["url"] == "https://www.furaffinity.net/view/222/"
    assert by["333"]["type"] == "Writing"                    # type from `content_type`
    assert [d["submission_id"] for d in out] == ["222", "333"]  # newest posted_at first


def test_discovered_skips_blank_ids():
    rows = [{"submission_id": "", "title": "x"}, {"submission_id": None, "title": "y"}]
    assert build_discovered([("fa", CFG_FA, rows)], set()) == []


# ── Art/text classification (Artwork full-gallery) ─────────────────

def test_classify_kind_content_pure_platforms():
    # Image-only platforms are art regardless of the type string.
    assert classify_kind("da", "") == "art"
    assert classify_kind("ik", "image") == "art"
    # Literature-only platforms are text regardless of the type string.
    assert classify_kind("ao3", "") == "text"
    assert classify_kind("sqw", "") == "text"
    assert classify_kind("wp", "") == "text"


def test_classify_kind_mixed_platforms_from_type_string():
    assert classify_kind("fa", "Artwork (Digital)") == "art"
    assert classify_kind("fa", "Story") == "text"
    assert classify_kind("ws", "visual") == "art"
    assert classify_kind("ws", "literary") == "text"
    assert classify_kind("sf", "Artwork") == "art"
    assert classify_kind("sf", "Writing") == "text"
    assert classify_kind("bsky", "post") == "art"


def test_classify_kind_text_hint_wins_over_art_hint():
    # A "story illustration" is still text — text hints take precedence.
    assert classify_kind("fa", "Story illustration") == "text"


def test_classify_kind_unknown_when_no_hint():
    assert classify_kind("fa", "") == "unknown"
    assert classify_kind("fa", "Music") == "unknown"


def test_build_discovered_tags_kind():
    rows_fa = [
        {"submission_id": 1, "title": "Art", "category": "Artwork (Digital)"},
        {"submission_id": 2, "title": "Tale", "category": "Story"},
    ]
    out = {d["submission_id"]: d for d in build_discovered([("fa", CFG_FA, rows_fa)], set())}
    assert out["1"]["kind"] == "art"
    assert out["2"]["kind"] == "text"


# ── 2.69.0: image-first platforms + image-aware classification ─────

CFG_MAST = {"id_col": "submission_id", "title_col": "title",
            "url_template": "https://mastodon.social/web/statuses/{id}"}


def test_classify_kind_image_first_platforms():
    # Pixiv + Instagram are image-first → art regardless of the type string.
    assert classify_kind("pix", "") == "art"
    assert classify_kind("ig", "") == "art"


def test_classify_kind_has_image_breaks_tie():
    # An inconclusive microblog post is classified by image presence.
    assert classify_kind("mast", "", has_image=True) == "art"
    assert classify_kind("mast", "", has_image=False) == "text"
    # No has_image signal → legacy "unknown" (backward compatible).
    assert classify_kind("mast", "") == "unknown"


def test_build_discovered_image_post_is_art_and_prefers_link():
    rows = [
        {"submission_id": 9, "title": "Toot pic", "thumbnail_url": "http://t/9.jpg",
         "link": "https://mastodon.social/@me/9", "posted_at": "2026-05-05"},
        {"submission_id": 10, "title": "Text toot", "posted_at": "2026-05-06"},
    ]
    out = {d["submission_id"]: d for d in build_discovered([("mast", CFG_MAST, rows)], set())}
    assert out["9"]["kind"] == "art"                          # image-bearing → art
    assert out["9"]["url"] == "https://mastodon.social/@me/9"  # stored link wins
    assert out["10"]["kind"] == "text"                        # no image → text
    # Falls back to url_template when no link is stored.
    assert out["10"]["url"] == "https://mastodon.social/web/statuses/10"
