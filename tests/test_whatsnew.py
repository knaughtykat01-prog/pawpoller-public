"""What's-new endpoint (2.134.0) — CHANGELOG parse + `since` filtering for the
in-app update popup."""
import config
from routes import whatsnew_api as wn


def test_parse_changelog_extracts_entries():
    text = ("# CL\n\n---\n\n## [2.2.0] - 2026-01-02 - Title B\n\nbody B\n\n---\n\n"
            "## [2.1.0] - 2026-01-01 - Title A\n\nbody A\n")
    entries = wn._parse_changelog(text)
    assert [e["version"] for e in entries] == ["2.2.0", "2.1.0"]
    assert entries[0]["header"].endswith("Title B")
    assert "body B" in entries[0]["body"] and "---" not in entries[0]["body"]


def test_whatsnew_since_filters(monkeypatch):
    text = ("## [2.3.0] - d - C\n\nc\n\n---\n\n## [2.2.0] - d - B\n\nb\n\n---\n\n"
            "## [2.1.0] - d - A\n\na\n")
    monkeypatch.setattr(wn, "_load_changelog", lambda: text)
    monkeypatch.setattr(config, "APP_VERSION", "2.3.0")
    r = wn.whatsnew(since="2.1.0")               # 2.1.0 (exclusive) → 2.3.0 (current)
    assert r["current"] == "2.3.0"
    assert [e["version"] for e in r["entries"]] == ["2.3.0", "2.2.0"]
    assert r["truncated"] is False


def test_whatsnew_first_run_empty(monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "2.3.0")
    assert wn.whatsnew(since="")["entries"] == []          # first run → show nothing


def test_whatsnew_already_current_empty(monkeypatch):
    text = "## [2.3.0] - d - C\n\nc\n"
    monkeypatch.setattr(wn, "_load_changelog", lambda: text)
    monkeypatch.setattr(config, "APP_VERSION", "2.3.0")
    assert wn.whatsnew(since="2.3.0")["entries"] == []     # already on current


# ── 2.156.0: the popup shows the SUMMARY, never the engineering body ──

def test_summary_is_the_leading_blockquote():
    body = ("> Stories and Artwork are now filters inside your Library.\n"
            "> Old links still work.\n\n"
            "Backlog L. `assemble_works` now projects `description`/`category`.")
    s = wn._summarize(body)
    assert s == "Stories and Artwork are now filters inside your Library. Old links still work."
    assert "assemble_works" not in s          # the internals must not leak into the popup


def test_summary_falls_back_to_first_paragraph_without_a_blockquote():
    # Every entry written before the convention has no blockquote.
    body = "First para, the gist.\n\nSecond para with `internals`."
    assert wn._summarize(body) == "First para, the gist."


def test_summary_fallback_strips_a_leading_heading_and_collapses_whitespace():
    assert wn._summarize("### Heading\nsome   gist\ntext") == "Heading some gist text"


def test_summary_handles_empty_body():
    assert wn._summarize("") == "" and wn._summarize(None) == ""


def test_blockquote_further_down_is_not_mistaken_for_the_summary():
    body = "Opening prose.\n\n> a quote used mid-entry, not the summary"
    assert wn._summarize(body) == "Opening prose."


def test_whatsnew_returns_summary_and_withholds_the_body(monkeypatch):
    text = ("## [2.2.0] - d - B\n\n> The friendly bit.\n\nThe `technical` bit.\n\n---\n\n"
            "## [2.1.0] - d - A\n\na\n")
    monkeypatch.setattr(wn, "_load_changelog", lambda: text)
    monkeypatch.setattr(config, "APP_VERSION", "2.2.0")
    e = wn.whatsnew(since="2.1.0")["entries"][0]
    assert e["summary"] == "The friendly bit."
    assert "body" not in e                    # the popup can't render what it never gets


def test_whatsnew_truncates(monkeypatch):
    text = "\n".join(f"## [1.0.{i}] - d - v{i}\n\nbody{i}\n\n---\n" for i in range(20, 0, -1))
    monkeypatch.setattr(wn, "_load_changelog", lambda: text)
    monkeypatch.setattr(config, "APP_VERSION", "1.0.20")
    r = wn.whatsnew(since="1.0.1")               # 1.0.2..1.0.20 = 19 → capped at 12
    assert len(r["entries"]) == wn._MAX_ENTRIES
    assert r["truncated"] is True
