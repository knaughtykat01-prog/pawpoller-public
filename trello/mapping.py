"""Settings for the Trello mirror, the commission board, and who owns them.

Settings shape (`specs/006-trello-board-mirror/data-model.md`)::

    trello: {
      poll_seconds, webhooks ("off" | "auto"), webhook_callback,
      commission_board_id, list_status: {list_id: status},
      owner_tag, member: {id, username, full_name}, inbox_id,
      migrated_005,
      board_id, list_map        # spec 005's shape, read only by the migration
    }

⚠ **The block syncs; the instance's own tag does not.** Both the desktop and the
server can hold the settings, and two instances each draining an outbox against
their own mirror would fight over every field. ``owner_tag`` names the one
instance allowed to talk to Trello; ``trello_instance_tag`` is per-device and in
``config.SYNC_EXCLUDE``. An instance whose tag does not match refuses, by name.

Lists are stored by **id**, never by name: renaming a list changes nothing.
"""
from __future__ import annotations

import uuid

import config
from clients.trello.client import TrelloClient
from database.commissions_queries import STATUSES

TAG_KEY = "trello_instance_tag"
MIN_POLL = 30

DEFAULTS = {
    "poll_seconds": 60,
    "webhooks": "off",
    "webhook_callback": "",
    "commission_board_id": "",
    "list_status": {},
    "owner_tag": "",
    "member": {},
    "inbox_id": "",
    "migrated_005": False,
    # spec 005 — kept so the one-time migration can read them
    "board_id": "",
    "list_map": {},
}


def get_config(settings: dict | None = None) -> dict:
    s = settings if settings is not None else config.get_settings()
    raw = s.get("trello") or {}
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in raw.items():
        if k in out:
            out[k] = v
    out["list_status"] = {str(k): v for k, v in (out.get("list_status") or {}).items()
                          if v in STATUSES}
    try:
        out["poll_seconds"] = max(MIN_POLL, int(out.get("poll_seconds") or DEFAULTS["poll_seconds"]))
    except (TypeError, ValueError):
        out["poll_seconds"] = DEFAULTS["poll_seconds"]
    out["webhooks"] = "auto" if out.get("webhooks") == "auto" else "off"
    return out


def save_config(**fields) -> dict:
    """Merge into the stored block. Only known keys are accepted."""
    cur = get_config()
    for k, v in fields.items():
        if k in DEFAULTS:
            cur[k] = v
    config.save_settings({"trello": cur})
    return get_config()


def credentials(settings: dict | None = None) -> tuple[str, str]:
    s = settings if settings is not None else config.get_settings()
    return str(s.get("trello_api_key") or "").strip(), str(s.get("trello_token") or "").strip()


def secret(settings: dict | None = None) -> str:
    s = settings if settings is not None else config.get_settings()
    return str(s.get("trello_secret") or "").strip()


def client_from_settings(settings: dict | None = None) -> TrelloClient:
    key, token = credentials(settings)
    return TrelloClient(key, token)


def is_configured(settings: dict | None = None) -> bool:
    key, token = credentials(settings)
    return bool(key and token)


def mode(settings: dict | None = None) -> str:
    """``webhook`` when Trello can tell us about changes, else ``poll`` (FR-018)."""
    cfg = get_config(settings)
    return "webhook" if (cfg["webhooks"] == "auto" and cfg["webhook_callback"]
                         and secret(settings)) else "poll"


def instance_tag() -> str:
    """This instance's identity. Created once, stored per-device, never synced."""
    s = config.get_settings()
    tag = str(s.get(TAG_KEY) or "").strip()
    if not tag:
        tag = uuid.uuid4().hex[:12]
        config.save_settings({TAG_KEY: tag})
    return tag


def claim() -> dict:
    """Take the Trello connection for this instance."""
    return save_config(owner_tag=instance_tag())


def owner_state(settings: dict | None = None) -> tuple[bool, str]:
    """``(is_owner, owner_tag)``. Unclaimed = whoever connects first."""
    owner = str(get_config(settings).get("owner_tag") or "")
    if not owner:
        return True, ""
    return owner == instance_tag(), owner


def status_for_list(list_id: str, settings: dict | None = None) -> str | None:
    """Which commission status a list means, or ``None`` if it means none.

    ⚠ ``None`` is a real answer: a card in an unmapped list keeps its last status
    (FR-031). Defaulting would move work the operator never moved.
    """
    return get_config(settings)["list_status"].get(list_id or "") or None


def lists_for_status(status: str, settings: dict | None = None) -> list[str]:
    return [lid for lid, st in get_config(settings)["list_status"].items() if st == status]
