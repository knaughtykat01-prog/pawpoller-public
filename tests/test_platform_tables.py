"""Every platform code has the labels the alerts and digests look up."""


def test_every_platform_has_an_emoji_and_a_name():
    # The cheap side of PYRIGHT: a new platform code once crashed the bot with KeyError: 'tg'.
    from database.accounts import PLATFORMS
    from polling.telegram import PLATFORM_EMOJI, PLATFORM_NAME
    assert [p for p in PLATFORMS if p not in PLATFORM_EMOJI or p not in PLATFORM_NAME] == []
