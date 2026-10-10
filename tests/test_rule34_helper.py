"""Rule34 by hand (spec 028 US7): what to paste, never a form sent."""
from pathlib import Path

from routes import masterpieces_api as mp

ROOT = Path(__file__).resolve().parents[1]


def test_tags_take_rule34s_form():
    assert mp.r34_tags(["artist:Inkwolf", "Sample Tag", "character:secondfur_(thirdfur)", "sample_tag", ""]) == [
        "inkwolf", "sample_tag", "secondfur_(thirdfur)"]


def test_rating_names_follow_the_ladder():
    from posting.platforms.base import rating_rank
    assert [mp._R34_RATING[rating_rank(r)] for r in ("general", "mature", "adult", "")] == [
        "safe", "questionable", "explicit", "safe"]


def test_under_18_is_refused(monkeypatch):
    import age_gate
    import pytest
    from fastapi import HTTPException
    monkeypatch.setattr(age_gate, "is_under_18", lambda settings=None: True)
    with pytest.raises(HTTPException) as e:
        mp.rule34_kit("Sample Piece")
    assert e.value.status_code == 403


def test_the_helper_never_sends_rule34s_form():
    """Constitution VII: a plain link and the clipboard, nothing posted to Rule34."""
    src = (ROOT / "frontend" / "js" / "masterpieces.js").read_text(encoding="utf-8")
    kit = src[src.index("async _openRule34Kit()"):src.index("_linkHtml() {")]
    assert "rel=\"noopener\"" in kit and "clipboard.writeText" in kit
    assert "fetch(" not in kit and "rule34.xxx" not in kit   # the URL comes from the server, opened by the person
    route = Path(mp.__file__).read_text(encoding="utf-8")
    body = route[route.index("def rule34_kit"):route.index('@masterpieces_router.post("/{name}/sync")')]
    assert "httpx" not in body and "Rule34Client" not in body
