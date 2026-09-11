"""Every Getting Started guide still ships, is small, is referenced, and carries alt text (4.30.0).

The Meta and Telegram guides have their own tests; this one covers every folder under
`frontend/img/guides/` at once so a platform illustrated later cannot ship a broken `img.src`
or leave a stray PNG behind. The stills come from real logins on each site (GUIDECAPS); every
e-mail address, token, one-time password, other user's handle and location was blurred before
the file was saved, and the raw captures never enter the repo.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "frontend", "js", "platform_guides.js")
IMG_ROOT = os.path.join(ROOT, "frontend", "img", "guides")
DOCS = os.path.join(ROOT, "docs")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 300 * 1024

# guides illustrated from the credential walks (each folder is the guide's platform code)
WALKED = ["ao3", "bsky", "da", "e621", "fa", "fbr", "fn", "ib", "ik", "mast", "ng", "pix", "sf", "sqw", "tum", "tw"]


def _read(p):
    return open(p, encoding="utf-8").read()


def _js_refs():
    return re.findall(r"img: \{ src: '/img/guides/([^']+)', alt: '((?:[^'\\]|\\.)*)' \}", _read(JS))


def _doc_refs():
    refs = set()
    for name in os.listdir(DOCS):
        if name.endswith(".md"):
            refs |= set(re.findall(r"frontend/img/guides/([^)\s]+)", _read(os.path.join(DOCS, name))))
    return refs


def test_every_walked_platform_is_illustrated():
    folders = {ref.split("/")[0] for ref, _ in _js_refs()}
    assert set(WALKED) <= folders, set(WALKED) - folders


def test_every_referenced_still_exists_and_is_a_small_png():
    refs = _js_refs()
    assert len(refs) >= 40
    for ref, _alt in refs:
        p = os.path.join(IMG_ROOT, *ref.split("/"))
        assert os.path.exists(p), ref
        with open(p, "rb") as fh:
            assert fh.read(8) == PNG_MAGIC, ref
        assert os.path.getsize(p) <= MAX_BYTES, (ref, os.path.getsize(p))


def test_every_still_has_alt_text():
    for ref, alt in _js_refs():
        assert len(alt) > 20, (ref, alt)


def test_no_orphan_stills_anywhere():
    used = {ref for ref, _ in _js_refs()} | _doc_refs()
    on_disk = set()
    for folder in os.listdir(IMG_ROOT):
        d = os.path.join(IMG_ROOT, folder)
        if os.path.isdir(d):
            on_disk |= {f"{folder}/{f}" for f in os.listdir(d) if f.endswith(".png")}
    assert on_disk <= used, on_disk - used


def test_the_sofurry_guide_asks_for_a_token_not_a_password():
    js = _read(JS)
    sf = js[js.index("\n    sf: {"):js.index("\n    },", js.index("\n    sf: {"))]
    assert "Personal Access Token" in sf and "settings/pat-create" in sf
    assert "Username + Password" not in sf
