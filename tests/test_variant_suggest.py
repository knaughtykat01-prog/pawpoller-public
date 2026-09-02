"""By-title variant family suggester (2.160.0).

Groups artworks that are the same PIECE in different renders (rough/final,
SFW/NSFW) — which the perceptual-hash duplicate finder can't, because they're
different images. Pure logic; no DB except the dismiss round-trip test.
"""
from database import variant_suggest as vs


# ── base_title: peel render/edit qualifiers ──

def test_base_title_strips_known_qualifiers():
    assert vs.base_title("Midnight Snack (Rough)") == "midnight snack"
    assert vs.base_title("Ki — Reference Sheet (SFW)") == "ki — reference sheet"
    assert vs.base_title("Buddies Sharing a Bed (Early Rough)") == "buddies sharing a bed"
    assert vs.base_title("Foo (Rough) (SFW)") == "foo"          # repeated peel


def test_base_title_keeps_a_real_title_that_ends_in_parens():
    # "for the Night" isn't a qualifier word, so the title stays whole — two
    # genuinely different pieces must not collapse on a coincidental suffix.
    assert vs.base_title("Buddies Sharing a Bed for the Night") == \
        "buddies sharing a bed for the night"
    assert vs.base_title("Study (in Blue)") == "study (in blue)"


def test_variant_key_slugifies():
    assert vs.variant_key("Early Rough") == "early-rough"
    assert vs.variant_key("NSFW") == "nsfw"
    assert vs.variant_key("") == "variant"


# ── suggest_families ──

def _items(*titles):
    return [{"name": t.replace(" ", "_"), "title": t, "views": i}
            for i, t in enumerate(titles)]


def test_groups_a_rough_and_final_into_one_family():
    fams = vs.suggest_families(_items("Midnight Snack", "Midnight Snack (Rough)"))
    assert len(fams) == 1
    fam = fams[0]
    assert fam["base"] == "midnight snack"
    hero = [m for m in fam["members"] if m["is_hero"]]
    assert len(hero) == 1 and hero[0]["title"] == "Midnight Snack"   # the clean title
    assert hero[0]["key"] == ""                                       # primary
    rough = [m for m in fam["members"] if not m["is_hero"]][0]
    assert rough["key"] == "rough" and rough["label"] == "Rough"


def test_singletons_are_not_families():
    assert vs.suggest_families(_items("A Lone Piece", "Another One")) == []


def test_sfw_nsfw_pair_groups_even_though_both_are_qualified():
    fams = vs.suggest_families(_items(
        "Ki — Reference Sheet (SFW)", "Ki — Reference Sheet (NSFW)"))
    assert len(fams) == 1
    members = fams[0]["members"]
    assert {m["key"] for m in members if not m["is_hero"]} <= {"sfw", "nsfw"}
    # Exactly one hero even when neither has a clean title (most-viewed wins).
    assert sum(1 for m in members if m["is_hero"]) == 1


def test_duplicate_qualifier_keys_are_disambiguated():
    # Two "(Rough)"s in one family must not collide on key 'rough'.
    fams = vs.suggest_families(_items(
        "Twins", "Twins (Rough)", "Twins (Rough)"))
    keys = [m["key"] for m in fams[0]["members"] if not m["is_hero"]]
    assert len(keys) == len(set(keys))     # all unique


def test_title_falls_back_to_name():
    # A missing title uses the folder name; two distinct names don't group, and
    # nothing crashes on the empty title.
    assert vs.suggest_families([
        {"name": "Solo Piece", "title": ""},
        {"name": "Unrelated", "title": ""},
    ]) == []
    # ...but if the names themselves share a base, the fallback still groups them.
    fams = vs.suggest_families([
        {"name": "Barn Owl", "title": ""},
        {"name": "Barn Owl (Rough)", "title": ""},
    ])
    assert len(fams) == 1 and fams[0]["base"] == "barn owl"


def test_dismissed_pair_is_not_suggested():
    items = _items("Cats", "Cats (Rough)")
    dismissed = {frozenset(("Cats", "Cats_(Rough)"))}
    assert vs.suggest_families(items, dismissed=dismissed) == []


def test_bigger_family_survives_partial_dismiss():
    items = _items("Trio", "Trio (Rough)", "Trio (Sketch)")
    # Only dismiss the rough<->sketch edge; both still pair with the hero.
    dismissed = {frozenset(("Trio_(Rough)", "Trio_(Sketch)"))}
    fams = vs.suggest_families(items, dismissed=dismissed)
    assert len(fams) == 1 and len(fams[0]["members"]) == 3


# ── dismiss persistence ──

def test_not_variant_round_trip(tmp_path, monkeypatch):
    import config
    from database.db import init_db, get_connection
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    init_db()
    conn = get_connection()
    try:
        assert vs.not_variant_pairs(conn) == set()
        vs.add_not_variant(conn, ["B_piece", "A_piece"])       # order-independent
        assert frozenset(("A_piece", "B_piece")) in vs.not_variant_pairs(conn)
        # Idempotent — re-adding the same pair adds nothing.
        assert vs.add_not_variant(conn, ["A_piece", "B_piece"]) == 0
    finally:
        conn.close()
