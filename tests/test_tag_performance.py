"""Tag performance analytics (gap-wave-6) — which tags earn engagement.

Deterministic, no AI: for each keyword the pollers captured, normalise every
piece carrying it against its OWN platform's median headline metric, then take
the median ratio as the tag's performance ``index`` (>1 = beats the platform's
typical piece). These lock in the ranking, the noise floor (min_works), the
platform scope, and the cross-platform tag normalisation.
"""
import json

from database.db import get_connection
from database import analytics_queries as aq


def _seed(conn):
    # Six IB pieces. Views: three at 100, three at 10 → platform median 55.
    #   popular   → the three 100-view pieces           (should index ~1.8, works 3)
    #   common    → mix of high and low                  (index < 1, works 5)
    #   unpopular → only the two lowest                  (works 2 → below default floor)
    rows = [
        (1, 100, ["popular", "common"]),
        (2, 100, ["popular", "common"]),
        (3, 100, ["popular"]),
        (4, 10,  ["rare_tag", "common"]),
        (5, 10,  ["unpopular", "common"]),
        (6, 10,  ["unpopular", "common"]),
    ]
    for sid, views, kws in rows:
        conn.execute(
            "INSERT INTO submissions (submission_id, title, views, favorites_count, "
            "comments_count, keywords) VALUES (?,?,?,?,?,?)",
            (sid, f"P{sid}", views, 1, 0, json.dumps(kws)))
    conn.commit()


def _by_tag(res):
    return {t["tag"]: t for t in res["tags"]}


def test_index_reflects_performance():
    conn = get_connection()
    try:
        _seed(conn)
        tags = _by_tag(aq.get_tag_performance(conn, min_works=3))
        assert "popular" in tags and "common" in tags
        assert tags["popular"]["works"] == 3
        assert tags["popular"]["index"] > 1.0        # beats the platform median
        assert tags["common"]["index"] < 1.0         # drags below it
        assert tags["popular"]["index"] > tags["common"]["index"]
    finally:
        conn.close()


def test_min_works_is_the_noise_floor():
    conn = get_connection()
    try:
        _seed(conn)
        # 'unpopular' is on 2 pieces → excluded at the default floor of 3…
        assert "unpopular" not in _by_tag(aq.get_tag_performance(conn, min_works=3))
        # …and included when the floor is lowered to 2.
        assert "unpopular" in _by_tag(aq.get_tag_performance(conn, min_works=2))
    finally:
        conn.close()


def test_platform_scope():
    conn = get_connection()
    try:
        _seed(conn)
        # Only IB is seeded, so scoping to FA yields nothing.
        assert aq.get_tag_performance(conn, min_works=1, platform="fa")["tags"] == []
        assert aq.get_tag_performance(conn, min_works=1, platform="ib")["tags"]
    finally:
        conn.close()


def test_norm_tag_merges_underscore_and_case():
    assert aq._norm_tag("Big_Muscle") == "big muscle"
    assert aq._norm_tag("  DubCon ") == "dubcon"
    assert aq._norm_tag("big_muscle") == aq._norm_tag("Big Muscle")


def test_fa_faceted_machine_tags_excluded():
    """FA auto-stamps u_/c_/t_/s_/g_ faceted atoms on every submission — they're
    not artist tags and must never surface in the ranking."""
    conn = get_connection()
    try:
        for sid in (1, 2, 3, 4):
            conn.execute(
                "INSERT INTO fa_submissions (submission_id, title, views, "
                "favorites_count, comments_count, keywords) VALUES (?,?,?,?,?,?)",
                (sid, f"F{sid}", 100, 5, 0,
                 json.dumps(["u_secondfur", "c_artwork_digital", "s_tiger",
                             "t_general", "harness"])))
        conn.commit()
        tags = _by_tag(aq.get_tag_performance(conn, min_works=3))
        assert "harness" in tags                 # a real artist tag survives
        for machine in ("u secondfur", "c artwork digital", "s tiger", "t general"):
            assert machine not in tags
        # _is_machine_tag is FA-scoped — the same shape on another platform stays.
        assert aq._is_machine_tag("fa", "u_secondfur") is True
        assert aq._is_machine_tag("sf", "u_secondfur") is False
        assert aq._is_machine_tag("fa", "harness") is False
    finally:
        conn.close()
