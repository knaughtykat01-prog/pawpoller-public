"""Saved descriptions and tag groups (spec 060 US3).

Two small lists in settings, inserted while editing a piece and managed in Settings → Saved text:

- ``GET/PUT /api/library/saved-descriptions`` — ``{items: [{id, name, text}]}``
- ``GET/PUT /api/library/tag-groups``         — ``{items: [{id, name, tags}]}``

A PUT saves the whole list. Caps are checked here, server side (plan: Settings caps), and the
PostyBirb import (``pb_import.apply_extras``) goes through the same ``clean``.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, HTTPException

import config

library_router = APIRouter(prefix="/api/library", tags=["library"])

MAX_ITEMS = 200
MAX_NAME = 80
MAX_TEXT = 20_000
MAX_TAGS = 300
MAX_TAG = 200
KEYS = {"descriptions": "saved_descriptions", "groups": "tag_groups"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_FROM = re.compile(r"^postybirb:[A-Za-z0-9_-]{1,64}$")


def clean(kind: str, items) -> list[dict]:
    """The list as it may be stored, or ValueError with a sentence the page can show."""
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError(f"You can keep up to {MAX_ITEMS}.")
    out, ids = [], set()
    for it in items:
        name = str(it.get("name") or "").strip() if isinstance(it, dict) else ""
        if not name or len(name) > MAX_NAME:
            raise ValueError(f"Each one needs a name, up to {MAX_NAME} characters.")
        iid = str(it.get("id") or "")
        if not _ID.fullmatch(iid) or iid in ids:
            iid = uuid.uuid4().hex[:12]
        ids.add(iid)
        row = {"id": iid, "name": name}
        if isinstance(it.get("from"), str) and _FROM.fullmatch(it["from"]):
            row["from"] = it["from"]                  # where an import came from, so it isn't brought in twice
        if kind == "descriptions":
            text = it.get("text")
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                raise ValueError(f"A saved description can be up to {MAX_TEXT:,} characters.")
            row["text"] = text
        else:
            tags = it.get("tags")
            if not isinstance(tags, list) or len(tags) > MAX_TAGS:
                raise ValueError(f"A tag group can hold up to {MAX_TAGS} tags.")
            kept, seen = [], set()
            for t in tags:
                s = str(t).strip()
                if len(s) > MAX_TAG:
                    raise ValueError(f"A tag can be up to {MAX_TAG} characters.")
                if s and s.lower() not in seen:
                    seen.add(s.lower())
                    kept.append(s)
            row["tags"] = kept
        out.append(row)
    return out


def load(kind: str) -> list[dict]:
    """What is stored, one item at a time: a bad item (a hand-edited file, a sync push) drops out on its
    own, rather than hiding the whole list and letting the next save wipe it."""
    items = config.get_settings().get(KEYS[kind])
    out = []
    for it in items[:MAX_ITEMS] if isinstance(items, list) else []:
        try:
            out += clean(kind, [it])
        except ValueError:
            continue
    return out


def save(kind: str, items) -> list[dict]:
    rows = clean(kind, items)
    config.save_settings({KEYS[kind]: rows})
    return rows


def merge_imported(kind: str, new: list[dict]) -> dict:
    """Add imported items (each with ``from``). One already brought in is skipped; a name in use gets
    " (PostyBirb)"; past the cap the rest are left out."""
    rows = load(kind)
    have_from = {r.get("from") for r in rows}
    names = {r["name"].lower() for r in rows}
    added = skipped = full = 0
    for it in new:
        if it.get("from") in have_from:
            skipped += 1
            continue
        if len(rows) >= MAX_ITEMS:
            full += 1
            continue
        base = str(it.get("name") or "").strip()[:MAX_NAME] or "Untitled"
        name, n = base, 1
        while name.lower() in names:
            tail = " (PostyBirb)" if n == 1 else f" (PostyBirb {n})"
            name, n = base[:MAX_NAME - len(tail)] + tail, n + 1
        names.add(name.lower())
        rows.append({**it, "name": name})
        added += 1
    save(kind, rows)
    return {"added": added, "skipped": skipped, "full": full}


def _put(kind: str, body: dict) -> dict:
    try:
        return {"items": save(kind, body.get("items") if isinstance(body, dict) else None)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@library_router.get("/saved-descriptions")
def get_descriptions():
    return {"items": load("descriptions")}


@library_router.put("/saved-descriptions")
def put_descriptions(body: dict):
    return _put("descriptions", body)


@library_router.get("/tag-groups")
def get_groups():
    return {"items": load("groups")}


@library_router.put("/tag-groups")
def put_groups(body: dict):
    return _put("groups", body)
