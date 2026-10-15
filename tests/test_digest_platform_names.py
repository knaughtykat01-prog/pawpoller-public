"""Every polled site has a digest name + emoji: the weekly digest crashed on 'tg'."""
from database import platform_metrics
from polling.telegram import PLATFORM_EMOJI, PLATFORM_NAME


def test_every_site_has_a_digest_name_and_emoji():
    codes = set(platform_metrics.ALL_CODES)
    assert codes <= set(PLATFORM_EMOJI), codes - set(PLATFORM_EMOJI)
    assert codes <= set(PLATFORM_NAME), codes - set(PLATFORM_NAME)
