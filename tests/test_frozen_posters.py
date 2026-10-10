"""Every poster the manager loads by name ships in the frozen builds (4.56.1).

`posting/manager.py::_POSTER_CLASSES` imports posters through importlib, by string. PyInstaller
only bundles what its static scan sees, so from 4.54.0 (when the table replaced the explicit
`from posting.platforms.x import ...` lines) to 4.56.0 every desktop / server-zip build shipped
without them, and every publish failed "No module named 'posting.platforms.furaffinity'".
Docker installs run from source, which is why the VM never showed it.
"""
from pathlib import Path

import pytest

from posting import manager

SPECS = ("pawpoller.spec", "pawpoller-server.spec")


def test_every_poster_lives_in_the_bundled_package():
    for code, (module, _cls) in manager._POSTER_CLASSES.items():
        assert module.startswith("posting.platforms."), f"{code}: {module} is outside the bundled package"


@pytest.mark.repo_only      # the server image leaves the .spec files out (.dockerignore)
def test_both_specs_bundle_the_whole_poster_package():
    for spec in SPECS:
        text = Path(spec).read_text(encoding="utf-8")
        assert "collect_submodules('posting.platforms')" in text, spec
        assert "*_POSTERS," in text, f"{spec}: collected but never passed to hiddenimports"


@pytest.mark.repo_only
def test_both_specs_bundle_the_gif_encoder():
    """Spec 030: posting/video_convert.py imports imageio_ffmpeg lazily, so both builds must name it
    (the contrib hook then ships its ffmpeg binary) or a packaged app can't send a GIF to Threads."""
    for spec in SPECS:
        assert "'imageio_ffmpeg'" in Path(spec).read_text(encoding="utf-8"), spec
