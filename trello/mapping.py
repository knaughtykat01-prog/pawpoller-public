"""The board, the status↔column map, and which instance owns them.

Settings shape (`specs/005-trello-commissions/data-model.md`)::

    trello: {
      board_id, board_name,
      list_map: {quote: "", accepted: "", wip: "", paid: "", delivered: ""},
      interval_min, owner_tag, first_sync_done
    }

⚠ **The block syncs; the instance's own tag does not.** `commissions` is SHR, so
the desktop and the server both hold the same commissions — and would both try to
drive the same board, each with its own baseline, fighting over every field. The
mapping block therefore syncs (both sides must agree *which* board and *who* owns
it) while ``trello_instance_tag`` is per-device and excluded from the settings
sync, exactly as `setup_mode` and the auth keys are. An instance whose tag does not
match ``owner_tag`` refuses to sync and names the holder.

Columns are stored by **id**, never by name: renaming a column on the board must
change nothing (spec edge case), and two columns can share a name.
"""
from __future__ import annotations

import uuid

import config
from database.commissions_queries import STATUSES

# Per-device, never synced. See SYNC_EXCLUDE in config.py.
TAG_KEY = "trello_instance_tag"

DEFAULTS = {
    "board_id": "",
    "board_name": "",
    "list_map": {s: "" for s in STATUSES},
    "interval_min": 30,
    "owner_tag": "",
    "first_sync_done": False,
}


def get_config(settings: dict | None = None) -> dict:
    s = settings if settings is not None else config.get_settings()
    raw = s.get("trello") or {}
    out = dict(DEFAULTS)
    out["list_map"] = dict(DEFAULTS["list_map"])
    for k, v in raw.items():
        if k == "list_map" and isinstance(v, dict):
            # Only the five known statuses. An unknown key in stored settings is
            # dropped rather than carried, so a typo cannot map a status that does
            # not exist and then look configured.
            for st in STATUSES:
                if st in v:
                    out["list_map"][st] = str(v[st] or "")
        elif k in out:
            out[k] = v
    out["interval_min"] = max(0, int(out.get("interval_min") or 0))
    out["first_sync_done"] = bool(out.get("first_sync_done"))
    return out


def save_config(**fields) -> dict:
    """Merge into the stored block. Only known keys are accepted."""
    cur = get_config()
    for k, v in fields.items():
        if k == "list_map" and isinstance(v, dict):
            for st in STATUSES:
                if st in v:
                    cur["list_map"][st] = str(v[st] or "")
        elif k in DEFAULTS:
            cur[k] = v
    cur["interval_min"] = max(0, int(cur.get("interval_min") or 0))
    config.save_settings({"trello": cur})
    return cur


def instance_tag() -> str:
    """This instance's identity. Created once, stored per-device, never synced."""
    s = config.get_settings()
    tag = str(s.get(TAG_KEY) or "").strip()
    if not tag:
        tag = uuid.uuid4().hex[:12]
        config.save_settings({TAG_KEY: tag})
    return tag


def claim(board_id: str, board_name: str = "") -> dict:
    """Take ownership of a board for this instance."""
    return save_config(board_id=board_id, board_name=board_name,
                       owner_tag=instance_tag(), first_sync_done=False)


def owner_state(settings: dict | None = None) -> tuple[bool, str]:
    """``(is_owner, owner_tag)``.

    An unclaimed board (no ``owner_tag``) is owned by whoever configures it — the
    first sync claims it. A board claimed by another instance is refused, by name,
    rather than silently fought over.
    """
    cfg = get_config(settings)
    owner = str(cfg.get("owner_tag") or "")
    if not owner:
        return True, ""
    return owner == instance_tag(), owner


def is_configured(settings: dict | None = None) -> bool:
    cfg = get_config(settings)
    return bool(cfg["board_id"]) and any(cfg["list_map"].values())


def status_for_list(list_id: str, settings: dict | None = None) -> str | None:
    """Which status a column means, or ``None`` if it means nothing.

    ⚠ ``None`` is a real answer and must stay one. FR-012: a card in an unmapped
    column leaves its commission's status alone and is reported. Defaulting to the
    first status here would move work the operator never moved.
    """
    if not list_id:
        return None
    for status, lid in get_config(settings)["list_map"].items():
        if lid and lid == list_id:
            return status
    return None


def list_for_status(status: str, settings: dict | None = None) -> str:
    """The column a status belongs in, or ``''`` when that status is unmapped."""
    return get_config(settings)["list_map"].get(status, "") or ""


def unmapped_statuses(settings: dict | None = None) -> list[str]:
    cfg = get_config(settings)
    return [s for s in STATUSES if not cfg["list_map"].get(s)]


def mapped_list_ids(settings: dict | None = None) -> set:
    return {lid for lid in get_config(settings)["list_map"].values() if lid}
