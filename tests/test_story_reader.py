"""Tests for the story archive reader."""

import pytest
from posting import story_reader


class TestArchivePathFallback:
    """The archive path must resolve to a GENERIC per-user folder on a shipped
    install — not the maintainer's `m_x/Archives/Complete_Stories` dev path, which
    isn't there and logged a "Story archive not found" warning for every user
    (2.162.0)."""

    def test_generic_default_when_no_custom_no_docker_no_dev(self, tmp_path, monkeypatch):
        import config
        config.save_settings({"posting_story_archive_path": ""})
        # Force the dev-checkout branch absent: point resource_path's parent at an
        # empty temp dir (no m_x sibling), and pin the per-user data dir.
        src = tmp_path / "src"; src.mkdir()
        monkeypatch.setattr(config, "resource_path", lambda rel=".": src / rel)
        monkeypatch.setattr(config, "APPDATA_DIR", tmp_path / "appdata")

        path = story_reader.get_archive_path()
        assert path == tmp_path / "appdata" / "story-archive"
        assert path.is_dir()          # created on resolve, so no "not found" warning

    def test_valid_custom_override_still_wins(self, tmp_path, monkeypatch):
        import config
        custom = tmp_path / "my-stories"; custom.mkdir()
        config.save_settings({"posting_story_archive_path": str(custom)})
        monkeypatch.setattr(config, "APPDATA_DIR", tmp_path / "appdata")
        assert story_reader.get_archive_path() == custom


class TestListStories:

    def test_list_stories_from_archive(self, story_archive):
        # Patch the archive path to our test fixture
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        stories = story_reader.list_stories()
        assert len(stories) == 1
        assert stories[0]["name"] == "Test_Story"
        assert stories[0]["has_manifest"] is True
        assert stories[0]["has_tags"] is True
        assert stories[0]["has_master"] is True

    def test_list_stories_empty_dir(self, tmp_path):
        import config
        config.save_settings({"posting_story_archive_path": str(tmp_path)})

        stories = story_reader.list_stories()
        assert stories == []


class TestLoadStory:

    def test_load_story_metadata(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        story = story_reader.load_story("Test_Story")
        assert story.name == "Test_Story"
        assert story.total_chapters == 2
        assert story.total_words == 100
        assert story.author == "TestAuthor"

    def test_load_story_chapters(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        story = story_reader.load_story("Test_Story")
        assert len(story.chapters) == 2
        assert story.chapters[0].title == "Beginning"
        assert story.chapters[0].word_count == 60
        assert story.chapters[1].title == "The End"

    def test_load_story_description(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        story = story_reader.load_story("Test_Story")
        assert "test story" in story.description.lower()

    def test_load_story_tags(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        story = story_reader.load_story("Test_Story")
        # Should have SF/default tags
        assert "sf" in story.tags_by_platform or "default" in story.tags_by_platform

    def test_load_story_not_found(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        with pytest.raises(FileNotFoundError):
            story_reader.load_story("Nonexistent_Story")

    def test_chapter_descriptions(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        story = story_reader.load_story("Test_Story")
        assert 1 in story.chapter_descriptions
        assert "chapter 1" in story.chapter_descriptions[1].lower()


class TestBuildPackage:

    def test_build_package_for_inkbunny(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        story = story_reader.load_story("Test_Story")
        pkg = story_reader.build_package(story, 1, "ib")

        assert pkg.story_name == "Test_Story"
        assert pkg.chapter_index == 1
        assert pkg.platform == "ib"
        assert "Beginning" in pkg.chapter_title
        assert pkg.word_count == 60
        # Should find BBCode file
        assert pkg.file_path is not None
        assert pkg.file_path.endswith(".txt")
        assert pkg.file_type == "bbcode"

    def test_build_package_for_sofurry(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        story = story_reader.load_story("Test_Story")
        pkg = story_reader.build_package(story, 1, "sf")

        assert pkg.platform == "sf"
        # Should find SoFurry HTML file
        assert pkg.file_path is not None
        assert pkg.file_path.endswith(".html")
        assert pkg.file_type == "html"

    def test_build_package_bluesky_no_file(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        story = story_reader.load_story("Test_Story")
        pkg = story_reader.build_package(story, 1, "bsky")

        assert pkg.platform == "bsky"
        assert pkg.file_path is None  # Bluesky doesn't upload files

    def test_build_package_title_override(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        story = story_reader.load_story("Test_Story")
        pkg = story_reader.build_package(story, 1, "ib", title_override="Custom Title")

        assert pkg.title == "Custom Title"

    def test_build_package_full_story(self, story_archive):
        import config
        config.save_settings({"posting_story_archive_path": str(story_archive)})

        story = story_reader.load_story("Test_Story")
        pkg = story_reader.build_package(story, 0, "ib")

        assert pkg.chapter_index == 0
        assert pkg.word_count == 100  # Total words
