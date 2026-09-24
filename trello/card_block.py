"""The machine-readable block appended to a card's description.

Trello cards have no price field. `PUT /cards/{id}` accepts ``name, desc, closed,
idMembers, idList, idLabels, idBoard, pos, due, start, dueComplete, subscribed,
address, locationName, coordinates, cover`` — nothing money-shaped. Custom fields
are the structurally right home and were rejected on availability: they are a
Power-Up, the allowance differs by plan tier, and a board where it is off would
drop the price with a successful-looking write (research R3).

So price, currency and the correlation key ride in a delimited block at the end of
the description, after the operator's own text.

⚠ **`strip()` is the load-bearing function in this module.** The block lives inside
a field the operator also edits, so the description comparison must remove it from
both sides before diffing. Skip that and every sync sees the description as changed
and raises a conflict against itself, for ever, on every card — the loudest wrong
behaviour this feature can produce.
"""
from __future__ import annotations

import re

OPEN = "<!-- pawpoller:do-not-edit"
CLOSE = "-->"

# Non-greedy, DOTALL: a description could in principle contain more than one
# block if a card were copied. Matching all of them and stripping every one is
# right — a stray second block is noise, not data.
_BLOCK_RE = re.compile(re.escape(OPEN) + r".*?" + re.escape(CLOSE), re.DOTALL)


def render(*, key: str, price=None, currency: str = "", url: str = "") -> str:
    """Build the block. Always ends without a trailing newline."""
    lines = [OPEN, f"key:   {key}"]
    if price not in (None, ""):
        money = f"{price}"
        if currency:
            money = f"{money} {currency}"
        lines.append(f"price: {money}")
    if url:
        lines.append(f"open:  {url}")
    lines.append(CLOSE)
    return "\n".join(lines)


def strip(desc: str) -> str:
    """The operator's own text, with every block removed.

    Trailing whitespace left behind by the removal is trimmed, because the block
    is appended after a blank line and leaving that blank line in would make a
    card that has a block differ from one that does not.
    """
    if not desc:
        return ""
    return _BLOCK_RE.sub("", desc).rstrip()


def parse(desc: str) -> dict:
    """Read the block back.

    Returns ``{}`` for a description with no block, or one whose block has been
    mangled by hand. ⚠ Never raises: a card whose block the operator edited is a
    card we rewrite on the next push, not a sync that falls over.
    """
    if not desc:
        return {}
    m = _BLOCK_RE.search(desc)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(0).splitlines():
        line = line.strip()
        if line in (OPEN, CLOSE) or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name, value = name.strip().lower(), value.strip()
        if name == "key":
            out["key"] = value
        elif name == "price":
            # "120.00 AUD" — amount first, currency optional.
            parts = value.split()
            if parts:
                try:
                    out["price"] = float(parts[0])
                except ValueError:
                    # A price we cannot read is not a price. Leaving it absent
                    # makes the field look unset rather than zero — zero is a
                    # number somebody might have meant.
                    pass
            if len(parts) > 1:
                out["currency"] = parts[1]
        elif name == "open":
            out["url"] = value
    return out


def apply(desc: str, block: str) -> str:
    """The operator's text with a freshly rendered block on the end."""
    body = strip(desc)
    return f"{body}\n\n{block}" if body else block


def make_key(client_name: str, created_at: str) -> str:
    """The correlation key written onto the card.

    It is the commission's natural key — the same pair `mirror/shr.py` matches on —
    and it is on the card so the mapping can be rebuilt if the local link table is
    ever lost.

    ⚠ It therefore contains the client's name, which is why this string may appear
    on the operator's own board and **never** in a log line, a test fixture or
    anything that reaches the repository.
    """
    return f"{client_name}|{created_at}"
