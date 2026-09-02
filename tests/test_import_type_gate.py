"""Artwork importer refuses non-image submissions with a clear message (2.158.1).

The ib/3811835 incident: a 'Writing - Document' submission's file_url is the
manuscript itself (text/plain), producing the misleading "IB may not expose a
direct image URL" error. The importer now recognises text/audio submission
types up front.
"""
import pytest

from posting.artwork_importer import _non_image_type_label, import_artwork
from database.db import get_connection


@pytest.mark.parametrize("row,expected", [
    ({"type_name": "Writing - Document"}, "Writing - Document"),
    ({"type_name": "Music - Audio"}, "Music - Audio"),
    ({"content_type": "text"}, "text"),
    ({"type_name": "Picture/Pinup"}, ""),          # image types pass
    ({"content_type": "photo"}, ""),
    ({}, ""),
])
def test_non_image_type_label(row, expected):
    assert _non_image_type_label(row) == expected


def test_import_refuses_writing_submission_with_clear_error():
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO submissions (submission_id, title, type_name, rating_name, "
        "thumb_url, url, account_id) VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("990990990", "A Manuscript", "Writing - Document", "Adult",
         "https://example.test/thumb.jpg", "https://inkbunny.net/s/990990990"))
    conn.commit()
    conn.close()
    with pytest.raises(ValueError) as e:
        import_artwork("ib", "990990990")
    msg = str(e.value)
    assert "Writing - Document" in msg
    assert "story" in msg.lower()
    assert "may not expose" not in msg      # the old misleading text is gone
