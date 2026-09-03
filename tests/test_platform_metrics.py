"""Conformance tests for the canonical platform metric registry.

A registry deduplicates a WRONG column name just as happily as a right one, so
the registry alone would not have prevented the incident it was built for:
``posting_queries`` asked AO3/SquidgeWorld for ``hits``/``kudos``, columns that
have never existed, and a bare ``except: continue`` swallowed the error — every
AO3 + SqW publication silently reported zero stats.

These tests are the actual guard. ``test_every_declared_column_exists`` fails
loudly the moment a registry entry names a column the schema doesn't have, and
``test_registry_covers_every_known_platform`` fails when a new platform is
wired up but left out of the registry (six of the fifteen old copies never
learned about FurryNetwork/Furbooru).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from database import platform_metrics as pm
from database.db import get_connection


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


@pytest.mark.parametrize("code", pm.ALL_CODES)
def test_every_declared_column_exists(code):
    """Every metric column the registry declares must exist in BOTH that
    platform's submissions table and its snapshots table — checked against a
    freshly-initialised schema, so the fresh-install path is covered too."""
    spec = pm.get(code)
    conn = get_connection()
    try:
        for table in (spec.table, spec.snapshots):
            actual = _columns(conn, table)
            assert actual, f"{code}: table {table} does not exist"
            assert spec.id_col in actual, f"{code}: {table} has no {spec.id_col}"
            missing = [c for c in spec.columns if c not in actual]
            assert not missing, (
                f"{code}: {table} is missing declared column(s) {missing}. "
                f"Declare the column names the SCHEMA uses, not the ones the "
                f"site's UI uses — site vocabulary belongs in `labels`."
            )
    finally:
        conn.close()


def test_registry_covers_every_known_platform():
    """The JS registry (frontend/js/platforms.js) and this one must list the
    same platform codes. Drift here is how the Overview ended up with no
    FurryNetwork or Furbooru tiles."""
    src = Path(__file__).resolve().parents[1] / "frontend" / "js" / "platforms.js"
    text = src.read_text(encoding="utf-8")
    # Match per LINE, not with a [^}]* class: every entry's emoji is a
    # unicode brace escape whose closing brace ends a naive match early, so
    # a postOnly flag sitting after it was never seen.
    entry_lines = [l for l in text.splitlines()
                   if re.search(r"\{\s*code:\s*'[a-z0-9]+'", l)]
    assert entry_lines, "could not parse any platform codes out of platforms.js"

    # POST-ONLY platforms are deliberately absent from THIS registry. It maps a
    # platform to the table holding its stats and the columns they live in, and
    # a post-only target has neither — Telegram is a broadcast channel, and the
    # Bot API exposes no per-post stats at all (view counts are MTProto-only).
    # Declaring a table it does not have would be fiction, and every aggregate
    # reading this registry would then query a table that does not exist.
    #
    # They still belong in the JS registry, which drives labels, logos and
    # pickers — without an entry there a platform renders as a bare code.
    def _code(line):
        return re.search(r"\{\s*code:\s*'([a-z0-9]+)'", line).group(1)

    post_only = {_code(l) for l in entry_lines
                 if re.search(r"postOnly:\s*true", l)}
    js_codes = {_code(l) for l in entry_lines} - post_only
    # No assertion that a post-only platform EXISTS. There was one, guarding
    # Telegram's absence from the metrics registry; Telegram graduated in 4.0.10
    # once its reactions were captured into a real stats table, so the set is
    # legitimately empty. The exclusion logic stays for the next broadcast-only
    # target.

    assert js_codes == set(pm.ALL_CODES), (
        f"registry drift — only in JS: {sorted(js_codes - set(pm.ALL_CODES))}; "
        f"only in Python: {sorted(set(pm.ALL_CODES) - js_codes)}"
    )


def test_accounts_platform_list_is_a_subset():
    """Anything the accounts layer POLLS must have metric metadata.

    Post-only platforms are exempt. An account is simply how you hold more than
    one of something — Telegram needs accounts so it can have several channels —
    but this registry maps a platform to the TABLE holding its stats, and a
    broadcast channel has none. Declaring one would be fiction, and every
    aggregate reading the registry would query a table that does not exist.
    """
    from database import accounts as accounts_db
    pollable = [p for p in accounts_db.PLATFORMS
                if p not in accounts_db.POST_ONLY_PLATFORMS]
    missing = [p for p in pollable if not pm.get(p)]
    assert not missing, f"platforms with accounts but no registry entry: {missing}"


def test_post_only_platforms_have_no_metrics_entry():
    """The other direction: a post-only platform must NOT gain a metrics entry
    without also gaining the stats tables it would then claim to have."""
    from database import accounts as accounts_db
    wrong = [p for p in accounts_db.POST_ONLY_PLATFORMS if pm.get(p)]
    assert not wrong, (
        f"{wrong} is declared post-only but has a metrics entry — either it now "
        f"has real stats tables (remove it from POST_ONLY_PLATFORMS) or the entry "
        f"is fiction (remove the entry)")


def test_score_platforms_declare_no_view_column():
    """The booru family reports a net up−down score, which may be NEGATIVE.
    Leaving `views` None is what stops an aggregate folding it into a view
    total (the link/masterpiece roll-ups used to do exactly that)."""
    for code in pm.SCORE_PLATFORMS:
        spec = pm.get(code)
        assert spec.views is None, f"{code} is a score platform but declares views={spec.views!r}"
        assert spec.score, f"{code} is a score platform but declares no score column"


def test_every_platform_has_a_family_and_some_metric():
    for code in pm.ALL_CODES:
        spec = pm.get(code)
        assert spec.family in ("views", "score", "engagement"), f"{code}: bad family"
        assert spec.columns, f"{code}: no metric columns at all"
        # A views-family platform must actually have a view column, or it is
        # really an engagement platform and would silently contribute 0.
        if spec.family == "views":
            assert spec.views, f"{code}: family=views but no views column"


def test_pooled_keeps_score_out_of_views():
    total = pm.pooled([
        ("fa", {"views": 50, "faves": 2, "comments": 0}),
        ("e621", {"score": 63, "faves": 161, "comments": 0}),
        ("fbr", {"score": -1, "faves": 2, "comments": 1}),
    ])
    assert total == {"views": 50, "score": 62, "faves": 165, "comments": 1}


def test_pooled_accepts_raw_column_keys():
    """Callers may pass a raw DB row (keyed by column name) instead of a
    canonical dict — e.g. Wattpad's reads/votes."""
    total = pm.pooled([
        ("wp", {"reads": 20, "votes": 3, "comments_count": 0}),
        ("tum", {"notes": 9}),
    ])
    assert total["views"] == 20
    assert total["faves"] == 12


def test_read_stats_round_trips_a_score_platform():
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO e621_submissions (submission_id, title, score, up_score,"
            " down_score, favorites_count, comments_count) VALUES (?,?,?,?,?,?,?)",
            ("1958732", "Somewhere", 63, 66, -3, 161, 0),
        )
        conn.commit()
        stats = pm.read_stats(conn, "e621", ["1958732"])
        assert stats["1958732"]["score"] == 63
        assert stats["1958732"]["faves"] == 161
        # No view metric on a score platform.
        assert stats["1958732"]["views"] is None
        # Raw column names ride along for back-compat.
        assert stats["1958732"]["up_score"] == 66
    finally:
        conn.close()


def test_read_stats_unknown_platform_is_empty_not_an_exception(caplog):
    conn = get_connection()
    try:
        assert pm.read_stats(conn, "nope", ["1"]) == {}
    finally:
        conn.close()
    assert "no registry entry" in caplog.text


def test_labels_carry_site_vocabulary_without_touching_sql():
    """AO3 says hits/kudos; the columns are views/favorites_count. The label is
    display-only — if it ever leaks into `columns` the SQL breaks again."""
    ao3 = pm.get("ao3")
    assert ao3.label_for("views") == "Hits"
    assert ao3.label_for("faves") == "Kudos"
    assert "hits" not in ao3.columns and "kudos" not in ao3.columns
    assert pm.get("e621").label_for("score") == "Score"
    assert pm.get("fa").label_for("faves") == "Favourites"
    assert pm.get("tg").label_for("faves") == "Reactions"


def test_every_label_is_title_case():
    """`label_for` only title-cases the FALLBACK, so an explicitly supplied
    label is rendered verbatim — a lowercase one reads as a typo next to
    "Hits" and "Notes" in the same table header row."""
    for spec in (pm.get(c) for c in pm.ALL_CODES):
        for key, text in spec.labels.items():
            assert text[:1].isupper(), f"{spec.code}.{key} label is {text!r}"
