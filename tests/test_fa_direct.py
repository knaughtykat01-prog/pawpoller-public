"""Tests for the direct-FA scraping fallback (FAExport-down path).

Verifies the regex parsers against representative FurAffinity (Beta) HTML so the
fallback produces the same dict shape the poller expects from FAExport.
"""

from clients.fa.client import FAClient


GALLERY_HTML = """
<section class="gallery">
  <figure id="sid-12345" class="t-image"><b><u>
    <a href="/view/12345/"><img src="//t.furaffinity.net/12345@200-1.jpg"></a>
  </u></b></figure>
  <figure id="sid-67890" class="t-image"><b><u>
    <a href="/view/67890/"><img src="//t.furaffinity.net/67890@200-1.jpg"></a>
  </u></b></figure>
</section>
"""

# Mirrors the CURRENT FurAffinity (Beta) submission-page markup (2026-06): stats
# live in <div class="submission-page-stats"> with <div title="X"><div>N</div>,
# the Favorites count is wrapped in a /favslist link, rating comes from the
# twitter meta pair, and tags are data-tag-name attributes.
SUBMISSION_HTML = """
<html><head><title>My Great Art by tester -- Fur Affinity [dot] net</title>
<meta name="twitter:label2" content="Rating" /><meta name="twitter:data2" content="General" />
</head>
<body>
<div class="submission-title"><h2>My Great Art</h2></div>
<div class="submission-page-stats">
  <div title="Views"><div>1,234</div><div class="highlight">Views</div></div>
  <div title="Comments"><div>7</div><div class="highlight">Comments</div></div>
  <div title="Favorites"><div><a href="/favslist/12345/" target="_blank">56</a></div></div>
</div>
<span class="popup_date" data-time="1700000000" title="Jun 1, 2026 03:14 PM">a month ago</span>
<img id="submissionImg" src="//d.furaffinity.net/art/tester/1700000000/full.jpg">
<span class="tags"><a href="javascript:void(0);" data-tag-name="fox">fox</a></span>
<span class="tags"><a href="javascript:void(0);" data-tag-name="anthro">anthro</a></span>
</body></html>
"""


class TestGalleryScrape:
    def test_sid_extraction(self):
        ids = [int(m) for m in FAClient._GALLERY_SID_RE.findall(GALLERY_HTML)]
        assert ids == [12345, 67890]

    def test_empty_page_yields_nothing(self):
        assert FAClient._GALLERY_SID_RE.findall("<section class='gallery'></section>") == []


class TestSubmissionParse:
    def setup_method(self):
        self.d = FAClient._parse_submission_html(SUBMISSION_HTML, 12345, "tester")

    def test_stats(self):
        assert self.d["views"] == 1234        # comma-formatted parsed
        assert self.d["favorites_count"] == 56
        assert self.d["comments_count"] == 7

    def test_metadata(self):
        assert self.d["submission_id"] == 12345
        assert self.d["title"] == "My Great Art"
        assert self.d["username"] == "tester"
        assert self.d["rating"] == "General"
        assert self.d["posted_at"] == "Jun 1, 2026 03:14 PM"
        assert self.d["keywords"] == ["fox", "anthro"]
        assert self.d["link"] == "https://www.furaffinity.net/view/12345/"
        assert self.d["thumbnail_url"].startswith("https://d.furaffinity.net/")

    def test_shape_matches_faexport_keys(self):
        # Same keys the FAExport path (_normalize_submission) produces, so the
        # poller / upsert_fa_submission are agnostic to the source.
        expected = {
            "submission_id", "title", "username", "posted_at", "category",
            "theme", "species", "gender", "rating", "thumbnail_url",
            "download_url", "description", "keywords", "link",
            "views", "favorites_count", "comments_count",
        }
        assert set(self.d.keys()) == expected

    def test_missing_stats_default_to_zero(self):
        d = FAClient._parse_submission_html("<html>no stats here</html>", 99, "tester")
        assert d["views"] == 0 and d["favorites_count"] == 0 and d["comments_count"] == 0
        assert d["submission_id"] == 99
