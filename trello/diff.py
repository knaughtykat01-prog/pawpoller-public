"""The three-way diff engine — where every guarantee in this feature actually lives.

**Pure.** Dictionaries in, decisions out. No database, no network, no clock, no
settings. If a test for this module ever needs one of those, the engine has grown
an I/O dependency it should not have.

The rule, per field:

====================  ====================  ============
local vs baseline     remote vs baseline    decision
====================  ====================  ============
same                  same                  ``NOOP``
differs               same                  ``PUSH``
same                  differs               ``PULL``
differs               differs               ``CONFLICT``
====================  ====================  ============

Two things about that table are the whole design:

1. **It needs a baseline.** Without one, "changed here" and "changed there" are
   indistinguishable from "we disagree", and the only available policy is a guess
   with a nice name — last-write-wins, comparing two clocks that were never
   synchronised, losing an edit with no trace. The failure is silent, which is the
   disqualifier.
2. **It is per FIELD, not per record.** A status dragged in Trello and a price
   edited in PawPoller are the ordinary Tuesday of this feature. Treating that as
   a conflict would train the operator to click through conflicts without reading
   them, which costs more than it saves the first time one is real.

⚠ ``description`` is compared with the card block stripped from both sides. The
block lives inside the field the operator edits, so comparing raw descriptions
makes every sync conflict with itself. `tests/test_trello_diff.py` pins it.
"""
from __future__ import annotations

from trello import card_block

PUSH = "push"
PULL = "pull"
CONFLICT = "conflict"
NOOP = "noop"

# Everything this feature will ever read or write. Anything not named here is
# never touched on either side — notes, artwork_name and deliver_sites stay
# PawPoller's, and attachments stay in PawPoller entirely.
FIELDS = ("status", "title", "description", "price", "currency", "due_date", "archived")


class Decision:
    """What to do about one field, and what to do it with."""

    __slots__ = ("field", "action", "value", "local", "remote", "baseline")

    def __init__(self, field: str, action: str, value=None, *,
                 local=None, remote=None, baseline=None):
        self.field = field
        self.action = action
        self.value = value          # what to write (PUSH/PULL); None for NOOP/CONFLICT
        self.local = local
        self.remote = remote
        self.baseline = baseline

    def __repr__(self) -> str:                                   # pragma: no cover
        return f"<Decision {self.field} {self.action}>"

    def __eq__(self, other):                                     # tests read better
        return (isinstance(other, Decision) and self.field == other.field
                and self.action == other.action and self.value == other.value)


# ── normalisation ────────────────────────────────────────────────────────────
#
# Both sides are reduced to the same seven keys with the same types before
# anything is compared. Comparing a float to a string, or a date to a datetime,
# would report a change on every sync — the kind of bug that looks like the
# feature working hard.

def _date_part(value) -> str:
    """Trello stores a datetime; a commission stores ``YYYY-MM-DD``."""
    s = str(value or "").strip()
    if not s:
        return ""
    return s.split("T")[0][:10]


def _money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _text(value) -> str:
    return (str(value) if value is not None else "").strip()


def card_title(client_name: str, description: str) -> str:
    """The card's name. Client first so the board is scannable by client."""
    client, desc = _text(client_name), _text(description)
    if client and desc:
        return f"{client} — {desc}"
    return client or desc


def normalise_local(commission: dict) -> dict:
    """A commission row reduced to the synced fields."""
    return {
        "status": _text(commission.get("status")),
        "title": card_title(commission.get("client_name"), commission.get("description")),
        "description": _text(commission.get("description")),
        "price": _money(commission.get("price")),
        "currency": _text(commission.get("currency")) or "USD",
        "due_date": _date_part(commission.get("due_date")),
        "archived": bool(commission.get("archived")),
    }


def normalise_remote(card: dict, status: str | None) -> dict:
    """A card reduced to the same shape.

    ``status`` is resolved by the caller through the column mapping, and is
    ``None`` when the card sits in a column mapped to nothing. That is carried
    through as ``None`` rather than guessed at — FR-012 says an unmapped column
    never changes a status, and the only way to honour that is to have no value.
    """
    block = card_block.parse(card.get("desc", ""))
    return {
        "status": status,
        "title": _text(card.get("name")),
        "description": card_block.strip(card.get("desc", "")),
        "price": _money(block.get("price")),
        "currency": _text(block.get("currency")) or "USD",
        "due_date": _date_part(card.get("due")),
        "archived": bool(card.get("closed")),
    }


# ── the engine ───────────────────────────────────────────────────────────────

def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return _money(a) == _money(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    return _text(a) == _text(b)


def decide(local: dict, remote: dict, baseline: dict,
           blocked: set | None = None) -> dict:
    """``{field: Decision}`` for every field that needs doing something about.

    ``blocked`` names fields with an unresolved conflict already recorded. They are
    left alone and reported as ``CONFLICT`` again — a conflict blocks its own field
    and nothing else (FR-017), so the rest of the commission keeps syncing while
    the operator decides.

    A field missing from ``baseline`` has never been agreed on. It is treated as
    "no agreement yet", so a side that holds a value pushes or pulls it, and both
    sides holding different values is — correctly — a conflict.
    """
    blocked = blocked or set()
    out: dict = {}

    for field in FIELDS:
        lv, rv = local.get(field), remote.get(field)

        # An unmapped column has no status to offer. Nothing to compare, nothing
        # to write — the caller reports the card instead.
        if field == "status" and rv is None:
            continue

        if field in blocked:
            out[field] = Decision(field, CONFLICT, local=lv, remote=rv,
                                  baseline=baseline.get(field))
            continue

        has_base = field in baseline
        bv = baseline.get(field)

        local_moved = not _same(lv, bv) if has_base else lv not in (None, "", 0, 0.0, False)
        remote_moved = not _same(rv, bv) if has_base else rv not in (None, "", 0, 0.0, False)

        if _same(lv, rv):
            # Both sides already agree. Whether they moved together or never moved
            # is irrelevant — there is nothing to write. This branch also quietly
            # heals a baseline that drifted.
            continue

        if local_moved and remote_moved:
            out[field] = Decision(field, CONFLICT, local=lv, remote=rv, baseline=bv)
        elif local_moved:
            out[field] = Decision(field, PUSH, lv, local=lv, remote=rv, baseline=bv)
        elif remote_moved:
            out[field] = Decision(field, PULL, rv, local=lv, remote=rv, baseline=bv)
        else:
            # Neither moved, yet they differ: the baseline equals both, which is
            # impossible, or a normalisation changed underfoot. Treat as a
            # conflict rather than picking — this branch should be unreachable and
            # loudly is not a silent overwrite if it ever is reached.
            out[field] = Decision(field, CONFLICT, local=lv, remote=rv, baseline=bv)

    return out


def card_fields_for(decisions: dict, local: dict) -> dict:
    """The Trello write for a set of PUSH decisions — only what changed.

    ⚠ Only the changed fields go in. Sending the whole card on a status-only move
    would rewrite ``name`` and ``desc`` with what we last read, reverting an edit
    made on the board between syncs (FR-007).
    """
    out: dict = {}
    for field, d in decisions.items():
        if d.action != PUSH:
            continue
        if field == "title":
            out["name"] = local["title"]
        elif field == "due_date":
            out["due"] = local["due_date"] or None
        elif field == "archived":
            out["closed"] = bool(local["archived"])
        # description, price and currency all land in `desc` — the caller renders
        # the block, because it needs the commission's key and url to do it.
    return out
