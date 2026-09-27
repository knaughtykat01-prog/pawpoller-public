"""C2 redesign phase 3 (spec docs/specs/c2_detail_redesign.md §8), 4.39.0.

Three pieces: tag chips drag to reorder, folded cards are remembered, and the
story board's "Needs attention" card stops listing failures a retry fixed.
The failure rule is tested for real against the database; the frontend wiring
is pinned by source string, the same contract style as test_board_pages.py.
"""
from __future__ import annotations

import re

from database import posting_queries


def _log(conn, rows):
    """rows: (platform, account_id, chapter, status, error, created_at)"""
    for platform, acct, ch, status, err, at in rows:
        conn.execute(
            "INSERT INTO posting_log (story_name, platform, account_id, chapter_index, action, "
            "status, error_message, created_at, content_type) "
            "VALUES ('Sample_Story', ?, ?, ?, 'post', ?, ?, ?, 'story')",
            (platform, acct, ch, status, err, at))
    conn.commit()


class TestOpenFailures:

    def test_a_failure_a_later_retry_fixed_is_not_reported(self, db_conn):
        _log(db_conn, [("ao3", 1, 1, "failed", "timeout", "2026-01-01 10:00:00"),
                       ("ao3", 1, 1, "success", "", "2026-01-01 11:00:00")])
        assert posting_queries.open_failures(db_conn, "Sample_Story") == []

    def test_repeats_collapse_into_one_row_with_a_count(self, db_conn):
        _log(db_conn, [("sf", 1, 0, "failed", "first", "2026-01-01 10:00:00"),
                       ("sf", 1, 0, "failed", "second", "2026-01-01 11:00:00"),
                       ("sf", 1, 0, "failed", "latest", "2026-01-01 12:00:00")])
        (row,) = posting_queries.open_failures(db_conn, "Sample_Story")
        assert row["failures"] == 3 and row["error_message"] == "latest"

    def test_only_failures_since_the_last_success_are_counted(self, db_conn):
        _log(db_conn, [("sf", 1, 0, "failed", "old", "2026-01-01 09:00:00"),
                       ("sf", 1, 0, "success", "", "2026-01-01 10:00:00"),
                       ("sf", 1, 0, "failed", "new", "2026-01-01 11:00:00")])
        (row,) = posting_queries.open_failures(db_conn, "Sample_Story")
        assert row["failures"] == 1 and row["error_message"] == "new"

    def test_targets_are_separate_by_site_account_and_chapter(self, db_conn):
        _log(db_conn, [("ao3", 1, 1, "failed", "x", "2026-01-01 10:00:00"),
                       ("ao3", 1, 2, "success", "", "2026-01-01 10:00:00"),
                       ("ao3", 2, 1, "failed", "y", "2026-01-01 10:00:00"),
                       ("wp", 1, 1, "success", "", "2026-01-01 10:00:00")])
        got = {(r["platform"], r["account_id"], r["chapter_index"])
               for r in posting_queries.open_failures(db_conn, "Sample_Story")}
        assert got == {("ao3", 1, 1), ("ao3", 2, 1)}

    def test_the_story_route_returns_them(self):
        src = open("routes/posting_api.py", encoding="utf-8").read()
        assert "posting_queries.open_failures(conn, story_name)" in src
        assert '"open_failures": open_failures' in src


def _src(path):
    return open(path, encoding="utf-8").read()


class TestFrontendWiring:

    def test_the_helper_is_loaded_after_both_boards_and_sortable(self):
        html = _src("frontend/index.html")
        i = html.index("/js/board_polish.js")
        assert html.index("/js/vendor/Sortable.min.js") < i
        assert html.index("/js/masterpieces.js") < i and html.index("/js/story_board.js") < i

    def test_both_boards_remember_folded_cards(self):
        assert "BoardPolish.collapse(root, 'mp')" in _src("frontend/js/masterpieces.js")
        assert "BoardPolish.collapse(root, 'sb')" in _src("frontend/js/story_board.js")

    def test_both_tag_strips_reorder_through_the_textarea(self):
        """The chips are a view of #mp-e-tags / #sb-e-tags; a drag must write the
        order back there or Save persists the old order."""
        mp = _src("frontend/js/masterpieces.js")
        sb = _src("frontend/js/story_board.js")
        assert "BoardPolish.chipSort(host, 'data-mp-chip-x', () => this._tagsFromTextarea(), list => this._setTags(list))" in mp
        assert "BoardPolish.chipSort(host, 'data-sb-chip-x', () => this._tagsList(), list => this._setTags(list))" in sb

    def test_reorder_has_a_keyboard_path_and_keeps_focus(self):
        js = _src("frontend/js/board_polish.js")
        assert "e.altKey" in js and "ArrowLeft" in js and "ArrowRight" in js
        assert "again.focus()" in js, "the moved chip keeps focus (spec §13)"

    def test_the_x_button_and_add_slot_do_not_start_a_drag(self):
        js = _src("frontend/js/board_polish.js")
        assert "filter: '.x, .tagchip-slot, .tag-empty'" in js

    def test_folding_is_a_real_button_with_state(self):
        js = _src("frontend/js/board_polish.js")
        assert "btn.type = 'button'" in js and "aria-expanded" in js
        assert re.search(r"\.card\.is-folded > :not\(\.sec-title\) \{ display: none; \}", _src("frontend/css/board.css"))

    def test_storage_failure_never_breaks_the_page(self):
        js = _src("frontend/js/board_polish.js")
        assert js.count("try {") >= 2 and "localStorage" in js

    def test_needs_attention_uses_the_resolved_rule_with_a_fallback(self):
        sb = _src("frontend/js/story_board.js")
        i = sb.index("_attentionHtml(name, d) {")
        body = sb[i:i + 4000]
        assert "d.open_failures" in body and "recent_log" in body, "falls back for an older server"
        assert 'data-post-action="update-single"' in body, "drift rows are actionable"
        assert body.index("bad.concat(drift, queued)") > 0, "worst first"
        assert "Nothing needs you." in body

    def test_drift_is_grouped_per_site_with_one_update_all(self):
        """A multi-chapter story drifted on a few sites was one row per chapter
        per site — found on a seeded 19-posting story. One row per site now."""
        sb = _src("frontend/js/story_board.js")
        i = sb.index("_attentionHtml(name, d) {")
        body = sb[i:i + 5000]
        assert "driftBy[p.platform]" in body
        assert 'data-post-action="update-all"' in body and "drift.length" in body


def test_move_tag_is_pure_and_bounded():
    """moveTag in board_polish.js, exercised through node if available."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        return
    script = (_src("frontend/js/board_polish.js").replace("window.BoardPolish", "globalThis.BP")
              + "\nconst l=['a','b','c'];"
              "console.log(JSON.stringify([BP.moveTag(l,'b',-1),BP.moveTag(l,'a',-1),BP.moveTag(l,'C',1),l]));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    assert out.strip() == '[["b","a","c"],["a","b","c"],["a","b","c"],["a","b","c"]]'
