"""The Overnight sheet names pieces by their title, not their folder (4.69.1)."""
from types import SimpleNamespace

from polling import overnight


def test_a_piece_is_named_by_its_title(monkeypatch):
    from posting import artwork_reader, story_reader
    monkeypatch.setattr(artwork_reader, "load_artwork", lambda n: SimpleNamespace(title="Sample Piece"))
    monkeypatch.setattr(story_reader, "load_story", lambda n: (_ for _ in ()).throw(FileNotFoundError(n)))
    assert overnight._named("sample_piece_v2") == "Sample Piece"


def test_a_story_is_named_by_its_title(monkeypatch):
    from posting import artwork_reader, story_reader
    monkeypatch.setattr(artwork_reader, "load_artwork", lambda n: (_ for _ in ()).throw(FileNotFoundError(n)))
    monkeypatch.setattr(story_reader, "load_story", lambda n: SimpleNamespace(title="Sample Story"))
    assert overnight._named("Sample_Story_Folder", "story") == "Sample Story"


def test_no_title_falls_back_to_the_folder(monkeypatch):
    from posting import artwork_reader, story_reader
    boom = lambda n: (_ for _ in ()).throw(FileNotFoundError(n))  # noqa: E731
    monkeypatch.setattr(artwork_reader, "load_artwork", boom)
    monkeypatch.setattr(story_reader, "load_story", boom)
    assert overnight._named("Inkwolf_Sketch") == "Inkwolf Sketch"


def test_the_sheet_has_a_close_on_top_and_stays_off_signup():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "frontend/js/overnight.js").read_text(encoding="utf-8")
    assert 'class="ov-x"' in js and "data-close" in js.split('class="ov-x"')[1][:60]
    assert "signup" in js.split("blocked()")[1][:120]
    css = (Path(__file__).resolve().parents[1] / "frontend/css/overnight.css").read_text(encoding="utf-8")
    assert "max-height: none;" not in css and "100dvh" in css


def test_a_tour_never_auto_starts_over_a_dialog():
    """Its blocker sits above every dialog and swallowed the taps on the Overnight sheet's close buttons."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "frontend/js/tour.js").read_text(encoding="utf-8")
    body = js[js.index("async function maybeAuto"):js.index("function skip(")]
    assert body.index(".modal-overlay.open, [role=dialog]") < body.index("begin(name, steps, { auto: true })")
    ov = (Path(__file__).resolve().parents[1] / "frontend/js/overnight.js").read_text(encoding="utf-8")
    assert ".pp-tour-blocker" in ov.split("const busy")[1][:400]
