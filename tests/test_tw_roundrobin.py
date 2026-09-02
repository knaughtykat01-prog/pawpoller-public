"""Round-robin X account selection (polling/roundrobin.py).

X shares one per-IP rate budget across a user's accounts and throttles after
~2 account-scrapes per window, so the scheduler polls only the N
least-recently-polled accounts each cycle and rotates the rest. The selection
helper is pure (accts + batch + last-poll map → accounts to poll), so most of
this is exercised without a DB; one test covers the poll-log query that feeds it.
"""

from polling.roundrobin import select_roundrobin, effective_batch


def _accts(*ids):
    return [{"account_id": i, "label": f"acct{i}"} for i in ids]


def test_batch_zero_polls_all():
    accts = _accts(1, 2, 3)
    assert select_roundrobin(accts, 0, {}) == accts


def test_batch_at_or_above_count_polls_all():
    accts = _accts(1, 2, 3)
    assert select_roundrobin(accts, 3, {}) == accts
    assert select_roundrobin(accts, 9, {}) == accts


def test_never_polled_accounts_sort_first():
    accts = _accts(1, 2, 3)
    # accounts 1 and 3 have been polled; 2 never has → 2 must be picked first
    last_poll = {1: "2026-07-13 10:00:00", 3: "2026-07-13 11:00:00"}
    picked = select_roundrobin(accts, 1, last_poll)
    assert [a["account_id"] for a in picked] == [2]


def test_oldest_polled_first_among_polled():
    accts = _accts(1, 2, 3)
    last_poll = {
        1: "2026-07-13 12:00:00",
        2: "2026-07-13 09:00:00",  # oldest
        3: "2026-07-13 11:00:00",
    }
    picked = select_roundrobin(accts, 2, last_poll)
    assert [a["account_id"] for a in picked] == [2, 3]  # oldest two, in order


def test_ties_break_by_account_id():
    accts = _accts(3, 1, 2)  # deliberately unsorted input
    same = "2026-07-13 10:00:00"
    last_poll = {1: same, 2: same, 3: same}
    picked = select_roundrobin(accts, 2, last_poll)
    assert [a["account_id"] for a in picked] == [1, 2]  # lowest ids win the tie


def test_returns_exactly_batch_size():
    accts = _accts(1, 2, 3, 4, 5)
    assert len(select_roundrobin(accts, 2, {})) == 2


def test_returned_list_is_a_copy_when_polling_all():
    accts = _accts(1, 2)
    out = select_roundrobin(accts, 0, {})
    out.append({"account_id": 99})
    assert len(accts) == 2  # caller's list untouched


def test_rotation_covers_all_accounts_over_cycles():
    # Simulate the scheduler: each cycle poll batch=2 of 3, then stamp them as
    # just-polled so the next cycle prefers the one left behind.
    accts = _accts(1, 2, 3)
    last_poll = {}
    clock = 0
    polled_counts = {1: 0, 2: 0, 3: 0}
    for _ in range(6):
        picked = select_roundrobin(accts, 2, last_poll)
        for a in picked:
            clock += 1
            last_poll[a["account_id"]] = f"2026-07-13 10:00:{clock:02d}"
            polled_counts[a["account_id"]] += 1
    # Over 6 cycles of 2/3, every account is polled and none is starved.
    assert all(c > 0 for c in polled_counts.values())
    assert max(polled_counts.values()) - min(polled_counts.values()) <= 1


def test_effective_batch_scraper_always_throttles():
    # A scraper (gallery-dl/GraphQL) is primary → shared per-IP budget → round-robin.
    assert effective_batch(2, official_primary=False, save_tokens=False) == 2
    assert effective_batch(2, official_primary=False, save_tokens=True) == 2


def test_effective_batch_official_primary_polls_all_by_default():
    # Only when the IP-agnostic official API is the PRIMARY do we poll every account.
    assert effective_batch(2, official_primary=True, save_tokens=False) == 0


def test_effective_batch_official_primary_throttles_when_saving_tokens():
    # User opted into throttling to spend fewer paid reads.
    assert effective_batch(2, official_primary=True, save_tokens=True) == 2


def test_save_tokens_preference_round_trips():
    from routes.api import get_preferences, save_preferences
    assert get_preferences()["tw_roundrobin_save_tokens"] is False
    save_preferences({"tw_roundrobin_save_tokens": True})
    assert get_preferences()["tw_roundrobin_save_tokens"] is True


def test_get_tw_last_poll_by_account_query():
    from database.db import get_connection
    from database import tw_queries

    conn = get_connection()
    try:
        # Two accounts logged; account 3 never logged → absent from the map.
        log1 = tw_queries.start_tw_poll_log(conn, account_id=1)
        tw_queries.finish_tw_poll_log(conn, log1, "success")
        log2 = tw_queries.start_tw_poll_log(conn, account_id=2)
        tw_queries.finish_tw_poll_log(conn, log2, "success")
        # A second poll for account 1 — MAX(started_at) should keep the latest.
        log1b = tw_queries.start_tw_poll_log(conn, account_id=1)
        tw_queries.finish_tw_poll_log(conn, log1b, "success")

        m = tw_queries.get_tw_last_poll_by_account(conn)
        assert set(m.keys()) == {1, 2}
        assert 3 not in m
        assert m[1] is not None and m[2] is not None
    finally:
        conn.close()
