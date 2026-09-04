"""The Meta (Threads / Instagram) setup guides carry screenshots of the real
developer dashboard (4.6.4). Every image a guide or doc points at must ship,
be a PNG, and stay small enough to live in the installer; the renderer must
draw them; and the two guides must describe the flow the real app uses
(Instagram login, tester role, token generator) rather than a guess.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR = os.path.join(ROOT, "frontend", "img", "guides", "meta")
JS = os.path.join(ROOT, "frontend", "js", "platform_guides.js")
CSS = os.path.join(ROOT, "frontend", "css", "guides.css")
DOCS = [os.path.join(ROOT, "docs", "INSTAGRAM_SETUP.md"), os.path.join(ROOT, "docs", "THREADS_SETUP.md")]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 300 * 1024


def _read(p):
    return open(p, encoding="utf-8").read()


def _guide_images():
    return sorted(set(re.findall(r"src: '/img/guides/meta/([^']+)'", _read(JS))))


class TestImagesShip:
    def test_every_guide_image_exists_and_is_a_small_png(self):
        names = _guide_images()
        assert names, "the guides reference no images"
        for n in names:
            p = os.path.join(IMG_DIR, n)
            assert os.path.exists(p), n
            with open(p, "rb") as fh:
                assert fh.read(8) == PNG_MAGIC, n
            assert os.path.getsize(p) <= MAX_BYTES, (n, os.path.getsize(p))

    def test_every_doc_image_exists(self):
        for d in DOCS:
            refs = re.findall(r"\]\(\.\./frontend/img/guides/meta/([^)]+)\)", _read(d))
            assert refs, d
            for n in refs:
                assert os.path.exists(os.path.join(IMG_DIR, n)), (d, n)

    def test_no_orphan_images(self):
        used = set(_guide_images())
        for d in DOCS:
            used |= set(re.findall(r"\]\(\.\./frontend/img/guides/meta/([^)]+)\)", _read(d)))
        on_disk = {f for f in os.listdir(IMG_DIR) if f.endswith(".png")}
        assert on_disk <= used, on_disk - used

    def test_every_image_has_alt_text(self):
        js = _read(JS)
        for m in re.finditer(r"img: \{ src: '/img/guides/meta/[^']+', alt: '([^']*)' \}", js):
            assert len(m.group(1)) > 20
        assert js.count("img: { src: '/img/guides/meta/") == len(re.findall(r"img: \{ src: '/img/guides/meta/[^']+', alt: '[^']{20,}' \}", js))


class TestRenderer:
    def test_step_renderer_draws_the_figure(self):
        js = _read(JS)
        assert 'class="guide-fig"' in js and 'loading="lazy"' in js
        assert "s.img && s.img.src" in js
        css = _read(CSS)
        assert ".guide-fig img" in css and ".guide-step-body" in css


class TestTheFlowIsTheRealOne:
    def test_instagram_login_not_facebook_login(self):
        js = _read(JS)
        ig = js[js.index("    ig: {"):js.index("    tg: {")]
        assert "API setup with Instagram login" in ig
        assert "Instagram Tester" in ig and "Apps and websites" in ig
        assert "Generate token" in ig and "Add account" in ig
        assert "instagram-api-setup.png" in ig

    def test_threads_uses_the_settings_tab_token_generator(self):
        js = _read(JS)
        thr = js[js.index("    thr: {"):js.index("    ig: {")]
        assert "Threads Tester" in thr and "Website permissions" in thr
        assert "User Token Generator" in thr and "Generate Access Token" in thr
        assert "threads-settings.png" in thr

    def test_both_say_leave_the_app_unpublished(self):
        js = _read(JS)
        for code in ("thr", "ig"):
            block = js[js.index("    %s: {" % code):]
            block = block[:block.index("\n    },\n")]
            assert "Unpublished" in block, code
