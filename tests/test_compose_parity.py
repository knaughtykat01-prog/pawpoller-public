"""The two compose files must differ only in how the image is obtained.

`docker-compose.yml` builds from source; `docker-compose.image.yml` pulls the
published multi-arch image. Everything else — ports, volumes, restart policy,
env_file — has to stay identical, because a self-hoster who switches between
them should get the same instance, not a subtly different one.

Duplication was chosen over a Compose override deliberately: removing a `build:`
key in an override needs `!reset` (Compose v2.24+), and an override that ADDS
`build:` would make the image file the default, which would change what
`docker compose up -d --build` does for anyone already deploying from source.
Two standalone files cost a little duplication and no behaviour change — this
test is what makes the duplication safe.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "docker-compose.yml"
IMAGE = REPO / "docker-compose.image.yml"


def _service(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_both_compose_files_exist():
    assert SOURCE.is_file(), "the build-from-source compose file is the documented default"
    assert IMAGE.is_file(), "the prebuilt-image compose file is what makes self-hosting one command"


def test_they_differ_only_in_build_versus_image():
    src = _service(SOURCE)["services"]["pawpoller"]
    img = _service(IMAGE)["services"]["pawpoller"]

    assert "build" in src and "image" not in src, "docker-compose.yml must build from source"
    assert "image" in img and "build" not in img, "docker-compose.image.yml must pull, not build"

    drift = {
        key: (src.get(key), img.get(key))
        for key in set(src) | set(img)
        if key not in ("build", "image") and src.get(key) != img.get(key)
    }
    assert not drift, (
        "the two compose files have drifted apart; a self-hoster switching "
        f"between them would get a different instance: {drift}"
    )


def test_the_named_volumes_match():
    """A mismatch here silently strands a user's data under a second volume
    name when they switch files — the worst possible failure of this pair."""
    assert _service(SOURCE).get("volumes") == _service(IMAGE).get("volumes")


def test_the_image_is_the_published_multi_arch_one():
    img = _service(IMAGE)["services"]["pawpoller"]["image"]
    assert img.startswith("ghcr.io/"), f"expected the published GHCR image, got {img!r}"
    assert "pawpoller" in img
