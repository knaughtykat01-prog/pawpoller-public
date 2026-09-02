"""Tests for the shared story-archive exclude rules (mirroring Stage 4).

The bug these exist to prevent is not "an exclude rule is wrong" — it is
**"the two directions disagree"**, which is what made a push-then-pull round
trip non-idempotent. So the tests that matter check the two directions against
each other, not either one against a fixed list.
"""
from __future__ import annotations

from deploy import archive_sync_rules as rules


# ── The asymmetry that Stage 4 exists to remove ───────────────

def test_the_two_directions_now_share_one_rule_set():
    """pawsync excluded Styled_HTML and pawpull did not; pawpull excluded
    Chapters_backup_* and pawsync did not. Both now import this module, so the
    predicate is the same object in both directions by construction."""
    from deploy import pawsync
    assert pawsync._rules is rules


def test_every_excluded_dir_is_also_excluded_by_the_tar_flags():
    """The remote pack renders the rules as tar --exclude. If a rule exists in
    Python but not in the flags, the pull carries what the push refuses — the
    original asymmetry, reintroduced one layer down."""
    flags = rules.tar_exclude_flags()
    for name in rules.EXCLUDE_DIR_NAMES:
        assert f"--exclude='{name}'" in flags, name
    for pattern in rules.EXCLUDE_GLOBS:
        assert f"--exclude='{pattern}'" in flags, pattern
    assert "*.bak.[0-9]*" in flags, "the .bak rule must reach the remote tar too"


# ── The rules themselves ──────────────────────────────────────

def test_excluded_directories_are_dropped_at_any_depth():
    for name in ("Backups", "Drafts", "Styled_HTML"):
        assert rules.is_excluded(f"{name}/x.md")
        assert rules.is_excluded(f"Chosen/{name}/x.md")
        assert rules.is_excluded(f"Chosen/Markdown/{name}/deep/x.md")


def test_chapters_backup_directories_are_dropped():
    assert rules.is_excluded("Chosen/Chapters_backup_20260101/ch1.md")
    assert not rules.is_excluded("Chosen/Chapters/ch1.md")


def test_bak_undo_files_are_dropped():
    """The class the artwork mirror has always dropped, and the story archive
    never did — the spec's 'the server has 3 extra .bak files'."""
    assert rules.is_excluded("Chosen/story.json.bak.1755000000")
    assert rules.is_excluded("Chosen/Markdown/MASTER.md.bak.1755000000")
    assert not rules.is_excluded("Chosen/story.json")


def test_canonical_and_derived_but_needed_files_still_travel():
    """Only derived-AND-unused is dropped. The posters upload these directly and
    the server cannot regenerate a PDF, so they must cross."""
    for path in ("Chosen/Markdown/MASTER.md",
                 "Chosen/story.json",
                 "Chosen/cover.png",
                 "Chosen/tags_upload.txt",
                 "Chosen/SquidgeWorld/ch1.html",
                 "Chosen/Chapters/SoFurry_HTML/ch1.html",
                 "Chosen/PDF/Chosen.pdf",
                 "Chosen/EPUB/Chosen.epub"):
        assert not rules.is_excluded(path), path


def test_a_name_that_merely_contains_an_excluded_word_is_kept():
    """Substring matching would eat real stories. The rule is per path
    component, so `Backups_Of_The_Heart` is a story, not a backup folder."""
    assert not rules.is_excluded("Backups_Of_The_Heart/story.json")
    assert not rules.is_excluded("Chosen/Drafts_Of_War.md")


def test_windows_and_posix_separators_agree():
    """The two callers produce different separators — pathlib on Windows,
    tar on the server."""
    assert rules.is_excluded(r"Chosen\Backups\x.md")
    assert rules.is_excluded("Chosen/Backups/x.md")


# ── The report ────────────────────────────────────────────────

def test_identical_archives_report_a_clean_round_trip():
    side = {"Chosen/story.json": 10, "Chosen/Markdown/MASTER.md": 200}
    report = rules.classify(side, dict(side))
    assert report["round_trip_clean"]
    assert report["identical"] == 2


def test_excluded_files_never_count_as_divergence():
    """The whole point: a .bak on one side only is not a conflict, it is a file
    that was never meant to cross."""
    local = {"Chosen/story.json": 10, "Chosen/story.json.bak.1755000000": 9}
    server = {"Chosen/story.json": 10}
    report = rules.classify(local, server)
    assert report["round_trip_clean"]
    assert report["local_only"] == []
    assert report["excluded_present"]["local"] == ["Chosen/story.json.bak.1755000000"]


def test_genuine_divergence_is_reported_in_full():
    local = {"Nesting_Season/story.json": 10, "Chosen/story.json": 10}
    server = {"Chosen/story.json": 44, "Old_Story/story.json": 5}
    report = rules.classify(local, server)
    assert report["local_only"] == ["Nesting_Season/story.json"]
    assert report["server_only"] == ["Old_Story/story.json"]
    assert report["size_differs"] == ["Chosen/story.json"]
    assert not report["round_trip_clean"]


def test_the_report_groups_by_story():
    assert rules.story_of("Nesting_Season/Markdown/MASTER.md") == "Nesting_Season"
    assert rules.story_of("loose_file.md") == "loose_file.md"
