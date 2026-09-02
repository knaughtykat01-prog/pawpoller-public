"""Tests for platform poster implementations with mocked HTTP."""

import pytest
import respx
import httpx

from posting.platforms.base import StoryUploadPackage, PostResult


# ── Inkbunny Poster Tests ────────────────────────────────────

class TestInkbunnyPoster:

    def _make_package(self, upload_file):
        return StoryUploadPackage(
            story_name="Test_Story",
            chapter_index=1,
            chapter_title="Chapter 1",
            platform="ib",
            title="Test Story — Chapter 1",
            description="A test story chapter.",
            tags=["furry", "anthro", "test", "story"],
            rating="adult",
            file_path=upload_file,
            file_type="bbcode",
            word_count=100,
        )

    @pytest.mark.asyncio
    @respx.mock
    async def test_post_success(self, upload_file):
        import config
        config.save_settings({"username": "testuser", "password": "testpass"})

        # Mock IB login
        respx.post(f"{config.INKBUNNY_API_BASE}/api_login.php").mock(
            return_value=httpx.Response(200, json={"sid": "test_sid_123", "user_id": 1, "ratingsmask": "11111"})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_userrating.php").mock(
            return_value=httpx.Response(200, json={"sid": "test_sid_123"})
        )
        # Mock search probe for ensure_session
        respx.post(f"{config.INKBUNNY_API_BASE}/api_search.php").mock(
            return_value=httpx.Response(200, json={"error_code": 2})  # Force fresh login
        )
        # Mock upload
        respx.post(f"{config.INKBUNNY_API_BASE}/api_upload.php").mock(
            return_value=httpx.Response(200, json={"submission_id": 99999})
        )
        # Mock edit
        respx.post(f"{config.INKBUNNY_API_BASE}/api_editsubmission.php").mock(
            return_value=httpx.Response(200, json={"submission_id": 99999})
        )

        from posting.platforms.inkbunny import InkbunnyPoster
        poster = InkbunnyPoster()
        # Pre-create client with SID already set — bypasses session_cache DB query
        from clients.ib.client import InkbunnyClient
        client = InkbunnyClient(username="testuser", password="testpass")
        client.sid = "test_sid_123"
        poster._client = client

        pkg = self._make_package(upload_file)
        result = await poster.post(pkg)

        assert result.success is True
        assert result.external_id == "99999"
        assert "inkbunny.net" in result.external_url
        assert result.duration_seconds >= 0

    def test_validate_insufficient_tags(self, upload_file):
        from posting.platforms.inkbunny import InkbunnyPoster
        poster = InkbunnyPoster()
        pkg = self._make_package(upload_file)
        pkg.tags = ["only", "two"]  # IB needs 4+

        errors = poster.validate(pkg)
        assert any("4 tags" in e for e in errors)

    def test_validate_missing_file(self):
        from posting.platforms.inkbunny import InkbunnyPoster
        poster = InkbunnyPoster()
        pkg = StoryUploadPackage(
            story_name="X", chapter_index=0, chapter_title="",
            platform="ib", title="T", description="D",
            tags=["a", "b", "c", "d"],
            file_path="/nonexistent/file.txt",
        )
        errors = poster.validate(pkg)
        assert any("not found" in e.lower() for e in errors)


# ── Bluesky Poster Tests ─────────────────────────────────────

class TestBlueskyPoster:

    def _make_package(self):
        return StoryUploadPackage(
            story_name="Test_Story",
            chapter_index=0,
            chapter_title="",
            platform="bsky",
            title="Test Story",
            description="A short announcement for the test story. Check it out!",
            tags=[],
            rating="adult",
        )

    @pytest.mark.asyncio
    @respx.mock
    async def test_post_success(self):
        import config
        config.save_settings({
            "bsky_identifier": "test.bsky.social",
            "bsky_app_password": "test-app-pass",
        })

        # Mock login
        respx.post("https://bsky.social/xrpc/com.atproto.server.createSession").mock(
            return_value=httpx.Response(200, json={
                "accessJwt": "test_jwt", "refreshJwt": "test_refresh",
                "did": "did:plc:test123", "handle": "test.bsky.social",
            })
        )
        # Mock session check
        respx.get("https://bsky.social/xrpc/com.atproto.server.getSession").mock(
            return_value=httpx.Response(200, json={"did": "did:plc:test123"})
        )
        # Mock createRecord
        respx.post("https://bsky.social/xrpc/com.atproto.repo.createRecord").mock(
            return_value=httpx.Response(200, json={
                "uri": "at://did:plc:test123/app.bsky.feed.post/abc123",
                "cid": "bafytest",
            })
        )

        from posting.platforms.bluesky import BlueskyPoster
        poster = BlueskyPoster()
        pkg = self._make_package()
        result = await poster.post(pkg)

        assert result.success is True
        assert "at://" in result.external_id
        assert "bsky.app" in result.external_url
        # >= 0, not > 0: time.perf_counter() granularity on Windows is ~16ms,
        # so a mocked HTTP call that completes faster gives duration = 0.0.
        # We just need the field populated, not measurably non-zero.
        assert result.duration_seconds >= 0

    def test_validate_empty_description(self):
        from posting.platforms.bluesky import BlueskyPoster
        poster = BlueskyPoster()
        pkg = self._make_package()
        pkg.description = ""

        errors = poster.validate(pkg)
        assert any("text" in e.lower() for e in errors)


# ── FurAffinity Poster Tests ─────────────────────────────────

class TestFurAffinityPoster:

    def test_validate_title_too_long(self, upload_file):
        from posting.platforms.furaffinity import FurAffinityPoster
        poster = FurAffinityPoster()
        pkg = StoryUploadPackage(
            story_name="X", chapter_index=0, chapter_title="",
            platform="fa",
            title="A" * 65,  # Over 60 chars
            description="D",
            tags=["a", "b", "c"],
            file_path=upload_file,
        )
        errors = poster.validate(pkg)
        assert any("60" in e for e in errors)

    def test_validate_insufficient_tags(self, upload_file):
        from posting.platforms.furaffinity import FurAffinityPoster
        poster = FurAffinityPoster()
        pkg = StoryUploadPackage(
            story_name="X", chapter_index=0, chapter_title="",
            platform="fa", title="T", description="D",
            tags=["only", "two"],
            file_path=upload_file,
        )
        errors = poster.validate(pkg)
        assert any("3 tags" in e for e in errors)

    def test_validate_tag_string_too_long(self, upload_file):
        from posting.platforms.furaffinity import FurAffinityPoster
        poster = FurAffinityPoster()
        pkg = StoryUploadPackage(
            story_name="X", chapter_index=0, chapter_title="",
            platform="fa", title="T", description="D",
            tags=["x" * 50 for _ in range(15)],  # 15 * 50 = 750 chars > 500
            file_path=upload_file,
        )
        errors = poster.validate(pkg)
        assert any("500" in e for e in errors)


# ── Weasyl Poster Tests ──────────────────────────────────────

class TestWeasylPoster:

    def test_validate_insufficient_tags(self, upload_file):
        from posting.platforms.weasyl import WeasylPoster
        poster = WeasylPoster()
        pkg = StoryUploadPackage(
            story_name="X", chapter_index=0, chapter_title="",
            platform="ws", title="T", description="D",
            tags=["only_one"],
            file_path=upload_file,
        )
        errors = poster.validate(pkg)
        assert any("2 tags" in e for e in errors)


# ── SoFurry Poster Tests ─────────────────────────────────────

class TestSoFurryPoster:

    def test_validate_file_too_large(self, tmp_path):
        from posting.platforms.sofurry import SoFurryPoster
        poster = SoFurryPoster()
        # Create a 600KB file (over SF's 512KB limit)
        big_file = tmp_path / "big.txt"
        big_file.write_bytes(b"x" * (600 * 1024))

        pkg = StoryUploadPackage(
            story_name="X", chapter_index=0, chapter_title="",
            platform="sf", title="T", description="D",
            tags=["a", "b"],
            file_path=str(big_file),
        )
        errors = poster.validate(pkg)
        assert any("512KB" in e for e in errors)


# ── Rating Conversion Tests ──────────────────────────────────

class TestRatingConversions:

    def test_ib_rating_adult(self):
        from posting.platforms.inkbunny import _rating_to_tags
        tags = _rating_to_tags("adult")
        assert tags["rating_tag_4"] == "yes"
        assert tags["rating_tag_5"] == "yes"

    def test_ib_rating_general(self):
        from posting.platforms.inkbunny import _rating_to_tags
        tags = _rating_to_tags("general")
        assert tags["rating_tag_2"] == "no"
        assert tags["rating_tag_4"] == "no"

    def test_fa_rating_adult(self):
        from posting.platforms.furaffinity import _rating_to_fa
        assert _rating_to_fa("adult") == "1"

    def test_fa_rating_mature(self):
        from posting.platforms.furaffinity import _rating_to_fa
        assert _rating_to_fa("mature") == "2"

    def test_fa_rating_general(self):
        from posting.platforms.furaffinity import _rating_to_fa
        assert _rating_to_fa("general") == "0"

    def test_ws_rating(self):
        from posting.platforms.weasyl import _rating_to_ws
        assert _rating_to_ws("adult") == 40
        assert _rating_to_ws("mature") == 30
        assert _rating_to_ws("general") == 10

    def test_sf_rating(self):
        from posting.platforms.sofurry import _rating_to_sf
        assert _rating_to_sf("adult") == 20
        assert _rating_to_sf("mature") == 10
        assert _rating_to_sf("general") == 0


# ── Base Class Tests ─────────────────────────────────────────

class TestBaseValidation:

    def test_validate_no_title(self, upload_file):
        from posting.platforms.inkbunny import InkbunnyPoster
        poster = InkbunnyPoster()
        pkg = StoryUploadPackage(
            story_name="X", chapter_index=0, chapter_title="",
            platform="ib", title="", description="D",
            tags=["a", "b", "c", "d"],
            file_path=upload_file,
        )
        errors = poster.validate(pkg)
        assert any("title" in e.lower() for e in errors)

    def test_timer_measures_time(self):
        import time
        from posting.platforms.base import PlatformPoster
        start = PlatformPoster._start_timer()
        time.sleep(0.05)
        elapsed = PlatformPoster._elapsed(start)
        assert elapsed >= 0.04  # Allow some variance
        assert elapsed < 1.0
