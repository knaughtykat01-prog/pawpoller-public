"""Orchestration: read the board once, ask the engine, apply, report.

Spec: `specs/005-trello-commissions/`. The decisions live in `trello/diff.py`; this
module does the I/O and the bookkeeping around them.

Three invariants that the rest of the file exists to keep:

1. **A baseline advances only after the write it describes succeeded** — per field,
   never for the batch. An interrupted run therefore leaves every unwritten field
   looking like a pending change, which is what it is (FR-021).
2. **A failed read unlinks nothing.** A card's absence means "deleted" only when
   the read that failed to contain it actually succeeded. Revoked credentials
   returning an error, or an empty body, is the cheap way to make every commission
   look deleted (research R7).
3. **Nothing here deletes anything**, on either side. Cards are archived;
   commissions whose card is gone are kept and unlinked.
"""
from __future__ import annotations

import logging

import config
from clients.trello.client import (TrelloAuthError, TrelloClient, TrelloError,
                                   TrelloRetryableError)
from database import commissions_queries as cq
from database import trello_queries as tq
from trello import card_block, diff, mapping

logger = logging.getLogger(__name__)

# A run that would unlink more than this many links, AND more than this fraction
# of them, stops and asks instead. Deliberately crude: it exists to catch the
# systemic case (a board emptied, a wrong board picked) rather than to be clever.
UNLINK_ASK_MIN = 3
UNLINK_ASK_FRACTION = 0.5


def _empty_report(board_id: str = "") -> dict:
    return {
        "applied": False, "board_id": board_id,
        "counts": {"created": 0, "moved": 0, "updated": 0, "archived": 0,
                   "unlinked": 0, "conflicted": 0, "skipped": 0, "candidates": 0},
        "created": [], "moved": [], "updated": [], "archived": [], "unlinked": [],
        "conflicted": [], "skipped": [], "candidates": [], "errors": [],
    }


def _count(report: dict) -> dict:
    for k in report["counts"]:
        report["counts"][k] = len(report.get(k) or [])
    return report


def credentials(settings: dict | None = None) -> tuple[str, str]:
    s = settings if settings is not None else config.get_settings()
    return str(s.get("trello_api_key") or "").strip(), str(s.get("trello_token") or "").strip()


def client_from_settings(settings: dict | None = None) -> TrelloClient:
    key, token = credentials(settings)
    return TrelloClient(key, token)


def _commission_url(commission: dict, settings: dict) -> str:
    """A deep link back, when this instance knows its own address. Cosmetic."""
    base = str(settings.get("posting_server_url") or "").strip().rstrip("/")
    cid = commission.get("id")
    return f"{base}/#/commissions/{cid}" if base and cid else ""


def _desc_for_card(commission: dict, local: dict, settings: dict) -> str:
    """The operator's description plus a freshly rendered block."""
    block = card_block.render(
        key=card_block.make_key(commission.get("client_name", ""),
                                commission.get("created_at", "")),
        price=local["price"], currency=local["currency"],
        url=_commission_url(commission, settings))
    return card_block.apply(local["description"], block)


def run_sync(conn, *, apply: bool = False, settings: dict | None = None,
             confirm_unlinks: bool = False) -> dict:
    """One sync run. ``apply=False`` computes everything and writes nothing."""
    settings = settings if settings is not None else config.get_settings()
    cfg = mapping.get_config(settings)
    report = _empty_report(cfg["board_id"])

    if not mapping.is_configured(settings):
        report["errors"].append(
            "Trello is not set up yet — pick a board and map your columns in "
            "Settings → Trello.")
        return _count(report)

    is_owner, owner = mapping.owner_state(settings)
    if not is_owner:
        # ⚠ Both instances hold the same commissions (they are SHR-mirrored), so
        # without this they would both drive the board, each against its own
        # baseline, and disagree on every field for ever.
        report["errors"].append(
            f"Another PawPoller instance ({owner}) owns this board. Only one "
            "instance can sync it.")
        return _count(report)

    # The first sync always previews, however it was called (FR-019).
    forced_preview = apply and not cfg["first_sync_done"]
    if forced_preview:
        apply = False
        report["errors"].append(
            "First sync — this is a preview. Nothing was written. Confirm to apply it.")

    try:
        client = client_from_settings(settings)
    except ValueError as e:
        report["errors"].append(str(e))
        return _count(report)

    # ── the one read ─────────────────────────────────────────────────────────
    try:
        cards = client.cards(cfg["board_id"])
    except TrelloAuthError as e:
        report["errors"].append(str(e))
        return _count(report)                      # unlinks NOTHING — research R7
    except (TrelloRetryableError, TrelloError) as e:
        report["errors"].append(str(e))
        return _count(report)

    by_card_id = {c["id"]: c for c in cards}
    mapped_lists = mapping.mapped_list_ids(settings)

    commissions = cq.list_commissions(conn, archived=False) + \
        cq.list_commissions(conn, archived=True)

    links = {(l["client_name"], l["created_at"]): l for l in tq.list_links(conn, cfg["board_id"])}
    claimed_cards: set = set()
    pending_unlinks: list = []

    for commission in commissions:
        key = (commission.get("client_name", ""), commission.get("created_at"))
        local = diff.normalise_local(commission)
        link = links.get(key)

        # ── repair path: the card names us but the local link is gone ────────
        if not link:
            wanted = card_block.make_key(key[0], key[1])
            for c in cards:
                if c["id"] in claimed_cards:
                    continue
                if card_block.parse(c["desc"]).get("key") == wanted:
                    link = {"card_id": c["id"], "board_id": cfg["board_id"],
                            "baseline": {}, "card_url": c["url"]}
                    if apply:
                        link = tq.create_link(
                            conn, client_name=key[0], created_at=key[1],
                            card_id=c["id"], board_id=cfg["board_id"], card_url=c["url"])
                    break

        # ── no card yet ──────────────────────────────────────────────────────
        if not link:
            if commission.get("archived"):
                continue                            # archived work gets no new card
            target_list = mapping.list_for_status(local["status"], settings)
            if not target_list:
                report["skipped"].append(
                    {"client_name": key[0], "created_at": key[1],
                     "reason": "status_unmapped", "status": local["status"]})
                continue
            entry = {"client_name": key[0], "created_at": key[1],
                     "to_list": local["status"]}
            if apply:
                try:
                    created = client.create_card(
                        id_list=target_list, name=local["title"],
                        desc=_desc_for_card(commission, local, settings),
                        due=local["due_date"])
                except TrelloError as e:
                    report["errors"].append(f"Could not create a card: {e}")
                    continue
                # Link AFTER the card exists, never before — a link naming a card
                # that was not created is worse than no link.
                tq.create_link(conn, client_name=key[0], created_at=key[1],
                               card_id=created["id"], board_id=cfg["board_id"],
                               card_url=created["url"], baseline=dict(local))
                claimed_cards.add(created["id"])
                entry["card_id"] = created["id"]
            report["created"].append(entry)
            continue

        card = by_card_id.get(link["card_id"])
        claimed_cards.add(link["card_id"])

        # ── the card is gone from a SUCCESSFUL read ──────────────────────────
        if card is None:
            if local["archived"]:
                # We archived it ourselves; Trello's default filter hides it.
                continue
            pending_unlinks.append({"client_name": key[0], "created_at": key[1],
                                    "reason": "card_deleted"})
            continue

        # ── compare ──────────────────────────────────────────────────────────
        status = mapping.status_for_list(card["id_list"], settings)
        if status is None:
            report["skipped"].append(
                {"client_name": key[0], "created_at": key[1],
                 "reason": "card_in_unmapped_column", "card_id": card["id"]})
        remote = diff.normalise_remote(card, status)
        blocked = tq.conflict_fields(conn, key[0], key[1])
        decisions = diff.decide(local, remote, link.get("baseline") or {}, blocked)

        _apply_decisions(conn, client, commission, local, remote, card, decisions,
                         key, settings, report, apply)

    # ── cards nobody claims ──────────────────────────────────────────────────
    for c in cards:
        if c["id"] in claimed_cards or c["id_list"] not in mapped_lists:
            continue
        report["candidates"].append(
            {"card_id": c["id"], "name": c["name"], "list_id": c["id_list"],
             "status": mapping.status_for_list(c["id_list"], settings) or ""})

    # ── unlinks, behind the mass-unlink guard ────────────────────────────────
    if pending_unlinks:
        total = max(1, len(links))
        mass = (len(pending_unlinks) >= UNLINK_ASK_MIN
                and len(pending_unlinks) / total >= UNLINK_ASK_FRACTION)
        if mass and not confirm_unlinks:
            report["errors"].append(
                f"{len(pending_unlinks)} of {total} linked cards are missing from the "
                "board. That usually means the wrong board, or a board that was "
                "emptied — nothing was unlinked. Confirm if it is really what you want.")
        else:
            for u in pending_unlinks:
                if apply:
                    tq.delete_link(conn, u["client_name"], u["created_at"])
                report["unlinked"].append(u)

    report["applied"] = bool(apply)
    if apply and not report["errors"]:
        mapping.save_config(first_sync_done=True)
    return _count(report)


def _apply_decisions(conn, client, commission, local, remote, card, decisions,
                     key, settings, report, apply) -> None:
    """Carry out one commission's decisions. Each write advances its own baseline."""
    client_name, created_at = key
    push_fields = {f: d for f, d in decisions.items() if d.action == diff.PUSH}
    pull_fields = {f: d for f, d in decisions.items() if d.action == diff.PULL}

    for field, d in decisions.items():
        if d.action != diff.CONFLICT:
            continue
        report["conflicted"].append(
            {"client_name": client_name, "created_at": created_at, "field": field})
        if apply:
            tq.open_conflict(conn, client_name=client_name, created_at=created_at,
                             field=field, baseline_value=d.baseline,
                             local_value=d.local, remote_value=d.remote)

    # ── push ─────────────────────────────────────────────────────────────────
    if push_fields:
        fields = diff.card_fields_for(push_fields, local)
        # description / price / currency all live in `desc`, which has to be
        # rendered from the commission rather than copied from a decision.
        if {"description", "price", "currency"} & set(push_fields):
            fields["desc"] = _desc_for_card(commission, local, settings)
        if "status" in push_fields:
            target = mapping.list_for_status(local["status"], settings)
            if target:
                fields["idList"] = target
                report["moved"].append(
                    {"card_id": card["id"], "from": remote["status"] or "",
                     "to": local["status"], "side": "local"})
            else:
                report["skipped"].append(
                    {"client_name": client_name, "created_at": created_at,
                     "reason": "status_unmapped", "status": local["status"]})
                push_fields.pop("status")
        if "archived" in push_fields and local["archived"]:
            report["archived"].append({"card_id": card["id"]})

        for f in push_fields:
            if f not in ("status", "archived"):
                report["updated"].append(
                    {"card_id": card["id"], "field": f, "side": "local"})

        if apply and fields:
            try:
                client.update_card(card["id"], **fields)
            except TrelloError as e:
                report["errors"].append(f"Could not update a card: {e}")
                return
            for f in push_fields:
                tq.set_baseline_field(conn, client_name, created_at, f, local[f])

    # ── pull ─────────────────────────────────────────────────────────────────
    if pull_fields:
        updates: dict = {}
        for field, d in pull_fields.items():
            if field == "status":
                updates["status"] = remote["status"]
                report["moved"].append(
                    {"card_id": card["id"], "from": local["status"],
                     "to": remote["status"], "side": "remote"})
                continue
            if field == "title":
                # ⚠ Splitting a retitled card back into client + description is
                # lossy. Only the description half is ever written back, and only
                # when the client half is untouched; otherwise it is a conflict
                # the operator resolves.
                prefix = f"{commission.get('client_name', '')} — "
                if remote["title"].startswith(prefix):
                    updates["description"] = remote["title"][len(prefix):]
                else:
                    report["conflicted"].append(
                        {"client_name": client_name, "created_at": created_at,
                         "field": "title"})
                    if apply:
                        tq.open_conflict(
                            conn, client_name=client_name, created_at=created_at,
                            field="title", baseline_value=d.baseline,
                            local_value=d.local, remote_value=d.remote)
                    continue
            elif field == "archived":
                updates["archived"] = 1 if remote["archived"] else 0
                report["archived"].append({"card_id": card["id"], "side": "remote"})
                continue
            else:
                updates[field] = remote[field]
            report["updated"].append(
                {"card_id": card["id"], "field": field, "side": "remote"})

        if apply and updates:
            cq.update_commission(conn, commission["id"], **updates)
            conn.commit()
            for f in pull_fields:
                if f == "title" and "description" not in updates:
                    continue
                tq.set_baseline_field(conn, client_name, created_at, f, remote[f])

    if apply:
        tq.touch_link(conn, client_name, created_at, card.get("last_activity", ""))
