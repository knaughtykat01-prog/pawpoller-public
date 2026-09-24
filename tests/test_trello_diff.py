"""The three-way diff engine (spec 005, research R4).

The engine is pure, so this file needs no database, no network and no clock — if
it ever does, the engine has grown an I/O dependency it should not have, and that
is itself the failure this file should report.

What is being pinned is one table, per field:

    local≠base  remote≠base   →  decision
    no          no               noop
    yes         no               push
    no          yes              pull
    yes         yes              conflict

and the two properties that make it worth having: it is per FIELD (so a status
dragged in Trello and a price edited here both apply), and a field whose two sides
disagree is never resolved by guessing.
"""
from __future__ import annotations

import pytest

from trello import card_block, diff

BASE = {"status": "wip", "title": "Sample Client — a piece", "description": "a piece",
        "price": 100.0, "currency": "AUD", "due_date": "2026-03-01", "archived": False}


def _sides(**changes):
    """(local, remote, baseline) with the named changes applied to one side."""
    local, remote = dict(BASE), dict(BASE)
    for field, (lv, rv) in changes.items():
        if lv is not ...:
            local[field] = lv
        if rv is not ...:
            remote[field] = rv
    return local, remote, dict(BASE)


class TestTheMatrix:

    def test_nothing_changed_decides_nothing(self):
        assert diff.decide(dict(BASE), dict(BASE), dict(BASE)) == {}

    @pytest.mark.parametrize("field,value", [
        ("status", "paid"), ("description", "a different piece"),
        ("price", 150.0), ("currency", "USD"), ("due_date", "2026-04-01"),
        ("archived", True),
    ])
    def test_only_local_changed_pushes(self, field, value):
        local, remote, base = _sides(**{field: (value, ...)})
        d = diff.decide(local, remote, base)
        assert d[field].action == diff.PUSH
        assert d[field].value == value

    @pytest.mark.parametrize("field,value", [
        ("status", "paid"), ("description", "a different piece"),
        ("price", 150.0), ("currency", "USD"), ("due_date", "2026-04-01"),
        ("archived", True),
    ])
    def test_only_remote_changed_pulls(self, field, value):
        local, remote, base = _sides(**{field: (..., value)})
        d = diff.decide(local, remote, base)
        assert d[field].action == diff.PULL
        assert d[field].value == value

    @pytest.mark.parametrize("field,lv,rv", [
        ("status", "paid", "delivered"),
        ("price", 150.0, 200.0),
        ("description", "mine", "theirs"),
        ("due_date", "2026-04-01", "2026-05-01"),
    ])
    def test_both_changed_conflicts_and_writes_neither(self, field, lv, rv):
        local, remote, base = _sides(**{field: (lv, rv)})
        d = diff.decide(local, remote, base)
        assert d[field].action == diff.CONFLICT
        assert d[field].value is None, "a conflict must carry no value to write"
        assert d[field].local == lv and d[field].remote == rv


class TestItIsPerFieldNotPerRecord:
    """The reason two-way sync is usable at all. A status dragged on the board and
    a price edited in PawPoller is the ordinary case, not a disagreement."""

    def test_different_fields_on_each_side_both_apply(self):
        local, remote, base = _sides(price=(150.0, ...), status=(..., "paid"))
        d = diff.decide(local, remote, base)
        assert d["price"].action == diff.PUSH
        assert d["status"].action == diff.PULL
        assert not any(x.action == diff.CONFLICT for x in d.values())

    def test_a_conflict_on_one_field_leaves_the_others_decided(self):
        local, remote, base = _sides(price=(150.0, 200.0), status=(..., "paid"))
        d = diff.decide(local, remote, base)
        assert d["price"].action == diff.CONFLICT
        assert d["status"].action == diff.PULL

    def test_a_blocked_field_is_reported_and_the_rest_still_move(self):
        local, remote, base = _sides(price=(150.0, 200.0), status=(..., "paid"))
        d = diff.decide(local, remote, base, blocked={"price"})
        assert d["price"].action == diff.CONFLICT
        assert d["status"].action == diff.PULL


class TestTheDescriptionBlockIsInvisibleToTheDiff:
    """⚠ Research R3. The block lives inside the field the operator edits, so a
    raw comparison makes every sync conflict with itself."""

    def test_a_card_whose_only_difference_is_the_block_is_a_no_op(self):
        card = {"name": BASE["title"],
                "desc": card_block.apply(BASE["description"],
                                         card_block.render(key="k", price=100,
                                                           currency="AUD")),
                "due": "2026-03-01T00:00:00.000Z", "closed": False, "id_list": "L"}
        remote = diff.normalise_remote(card, "wip")
        assert diff.decide(dict(BASE), remote, dict(BASE)) == {}

    def test_the_price_is_read_out_of_the_block(self):
        card = {"name": BASE["title"],
                "desc": card_block.apply("a piece",
                                         card_block.render(key="k", price=175,
                                                           currency="AUD")),
                "due": "2026-03-01", "closed": False, "id_list": "L"}
        remote = diff.normalise_remote(card, "wip")
        d = diff.decide(dict(BASE), remote, dict(BASE))
        assert d["price"].action == diff.PULL and d["price"].value == 175.0


class TestNormalisation:
    """Comparing a float to a string, or a date to a datetime, reports a change on
    every sync — the kind of bug that looks like the feature working hard."""

    def test_trellos_datetime_compares_against_a_plain_date(self):
        card = {"name": BASE["title"], "desc": "a piece",
                "due": "2026-03-01T13:00:00.000Z", "closed": False, "id_list": "L"}
        remote = diff.normalise_remote(card, "wip")
        assert remote["due_date"] == "2026-03-01"

    def test_a_price_typed_as_a_string_is_not_a_change(self):
        local = dict(BASE, price="100")
        assert "price" not in diff.decide(local, dict(BASE), dict(BASE))

    def test_money_compares_to_two_places(self):
        local = dict(BASE, price=100.004)
        assert "price" not in diff.decide(local, dict(BASE), dict(BASE))

    def test_the_title_is_the_client_then_the_description(self):
        assert diff.card_title("Sample Client", "a piece") == "Sample Client — a piece"
        assert diff.card_title("Sample Client", "") == "Sample Client"


class TestAnUnmappedColumnNeverMovesWork:
    """FR-012. `None` is a real answer and has to stay one."""

    def test_a_card_in_an_unmapped_column_yields_no_status_decision(self):
        local, remote, base = _sides()
        remote["status"] = None
        assert "status" not in diff.decide(local, remote, base)

    def test_the_other_fields_of_such_a_card_still_sync(self):
        local, remote, base = _sides(price=(..., 250.0))
        remote["status"] = None
        d = diff.decide(local, remote, base)
        assert "status" not in d
        assert d["price"].action == diff.PULL


class TestNoBaselineYet:
    """A field never agreed on. It must not be read as 'both sides changed' when
    only one holds anything, or a first sync would conflict on every field."""

    def test_one_side_holding_a_value_wins(self):
        local, remote = dict(BASE), dict(BASE, price=0.0)
        d = diff.decide(local, remote, {})
        assert d["price"].action == diff.PUSH

    def test_both_sides_holding_different_values_is_a_conflict(self):
        local, remote = dict(BASE, price=100.0), dict(BASE, price=200.0)
        assert diff.decide(local, remote, {})["price"].action == diff.CONFLICT

    def test_agreeing_without_a_baseline_decides_nothing(self):
        assert diff.decide(dict(BASE), dict(BASE), {}) == {}


class TestTheCardWriteCarriesOnlyWhatChanged:
    """FR-007. Sending the whole card on a status-only move would rewrite name and
    desc with what we last read, reverting an edit made on the board between syncs."""

    def test_a_status_only_change_writes_no_name_or_desc(self):
        local, remote, base = _sides(status=("paid", ...))
        fields = diff.card_fields_for(diff.decide(local, remote, base), local)
        assert "name" not in fields and "desc" not in fields

    def test_a_title_change_writes_the_name(self):
        local, remote, base = _sides(title=("Sample Client — new", ...))
        fields = diff.card_fields_for(diff.decide(local, remote, base), local)
        assert fields["name"] == "Sample Client — new"

    def test_a_pull_decision_never_becomes_a_card_write(self):
        local, remote, base = _sides(title=(..., "theirs"))
        assert diff.card_fields_for(diff.decide(local, remote, base), local) == {}


def test_the_engine_touches_nothing_but_its_arguments():
    """Pure. If this ever fails, the module has grown state."""
    local, remote, base = dict(BASE), dict(BASE, price=1.0), dict(BASE)
    before = (dict(local), dict(remote), dict(base))
    diff.decide(local, remote, base)
    assert (local, remote, base) == before
