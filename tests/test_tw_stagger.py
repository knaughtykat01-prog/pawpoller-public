"""X account stagger — polling/rate_limit.tw_account_stagger.

Verifies the burst-of-N sequencing that keeps a poll-all-accounts cycle under
X's per-IP throttle: no wait for the first burst, then a gap between bursts.
The subprocess/real-sleep boundary is injected (sleep_fn) so tests are instant.
"""

import pytest

from polling.rate_limit import tw_account_stagger


def _recorder():
    calls = []

    async def fake_sleep(s):
        calls.append(s)

    return calls, fake_sleep


@pytest.mark.asyncio
async def test_noop_for_non_tw_platform():
    calls, sleep = _recorder()
    for pc in range(5):
        await tw_account_stagger("bsky", pc, {}, sleep_fn=sleep)
    assert calls == []


@pytest.mark.asyncio
async def test_first_burst_never_sleeps():
    # every=2 default → accounts 0 and 1 are the first burst, no wait.
    calls, sleep = _recorder()
    await tw_account_stagger("tw", 0, {}, sleep_fn=sleep)
    await tw_account_stagger("tw", 1, {}, sleep_fn=sleep)
    assert calls == []


@pytest.mark.asyncio
async def test_sleeps_at_burst_boundary():
    calls, sleep = _recorder()
    await tw_account_stagger("tw", 2, {"tw_account_stagger_seconds": 480}, sleep_fn=sleep)
    assert calls == [480.0]


@pytest.mark.asyncio
async def test_burst_pattern_over_five_accounts():
    # every=2, gap=100: waits only before account index 2 and 4 (bursts of 2).
    calls, sleep = _recorder()
    settings = {"tw_account_stagger_seconds": 100}
    for pc in range(5):
        await tw_account_stagger("tw", pc, settings, sleep_fn=sleep)
    assert calls == [100.0, 100.0]


@pytest.mark.asyncio
async def test_three_accounts_one_gap():
    # The real case: 3 X accounts → exactly one 8-min gap (burst {0,1}, gap, {2}).
    calls, sleep = _recorder()
    settings = {"tw_account_stagger_seconds": 480}
    for pc in range(3):
        await tw_account_stagger("tw", pc, settings, sleep_fn=sleep)
    assert calls == [480.0]


@pytest.mark.asyncio
async def test_disabled_when_gap_zero():
    calls, sleep = _recorder()
    for pc in range(5):
        await tw_account_stagger("tw", pc, {"tw_account_stagger_seconds": 0}, sleep_fn=sleep)
    assert calls == []


@pytest.mark.asyncio
async def test_respects_per_user_setting():
    calls, sleep = _recorder()
    await tw_account_stagger("tw", 2, {"tw_account_stagger_seconds": 42}, sleep_fn=sleep)
    assert calls == [42.0]
