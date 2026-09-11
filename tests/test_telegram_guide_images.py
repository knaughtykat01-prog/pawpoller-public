"""The Telegram guide's stills (4.29.3) ship, are small, are referenced, and carry alt text.

Same contract as the Meta guide images: every `img.src` in platform_guides.js and every image the
setup doc embeds exists as a PNG under 300 KB, nothing on disk is orphaned, and every alt text says
something. The stills come from the operator's own screen recordings with tokens, ids, names and
artwork blurred before they were saved — the raw frames never enter the repo.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "frontend", "js", "platform_guides.js")
IMG_DIR = os.path.join(ROOT, "frontend", "img", "guides", "telegram")
DOC = os.path.join(ROOT, "docs", "TELEGRAM_SETUP.md")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 300 * 1024


def _read(p):
    return open(p, encoding="utf-8").read()


def _guide_images():
    return sorted(set(re.findall(r"src: '/img/guides/telegram/([^']+)'", _read(JS))))


def _doc_images():
    return sorted(set(re.findall(r"\]\(\.\./frontend/img/guides/telegram/([^)]+)\)", _read(DOC))))


def test_the_guide_and_the_doc_reference_stills():
    assert len(_guide_images()) >= 4
    assert len(_doc_images()) >= 15


def test_every_referenced_still_exists_and_is_a_small_png():
    for n in set(_guide_images()) | set(_doc_images()):
        p = os.path.join(IMG_DIR, n)
        assert os.path.exists(p), n
        with open(p, "rb") as fh:
            assert fh.read(8) == PNG_MAGIC, n
        assert os.path.getsize(p) <= MAX_BYTES, (n, os.path.getsize(p))


def test_no_orphan_stills():
    used = set(_guide_images()) | set(_doc_images())
    on_disk = {f for f in os.listdir(IMG_DIR) if f.endswith(".png")}
    assert on_disk <= used, on_disk - used


def test_every_guide_still_has_alt_text():
    js = _read(JS)
    entries = re.findall(r"img: \{ src: '/img/guides/telegram/[^']+', alt: '((?:[^'\\]|\\.)*)' \}", js)
    assert len(entries) == len(_guide_images())
    for alt in entries:
        assert len(alt) > 20, alt
