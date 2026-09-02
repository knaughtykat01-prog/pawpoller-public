"""Commissions — CRUD for the lightweight client/commission tracker
(gap-wave-5 §4). One self-contained table; no members/rollup (that's Collections'
job). Money is data only. See docs/specs/gap_wave5.md.
"""
from __future__ import annotations

import json
import sqlite3

# Status lifecycle. Validated in the API handlers so a bad value can't land.
STATUSES = ("quote", "accepted", "wip", "paid", "delivered")


def _clean_sites(deliver_sites) -> str:
    """Normalise deliver_sites to a JSON array string of platform codes.
    Accepts a list or a JSON string; drops anything non-string."""
    if isinstance(deliver_sites, str):
        try:
            deliver_sites = json.loads(deliver_sites) if deliver_sites else []
        except (json.JSONDecodeError, ValueError):
            deliver_sites = []
    if not isinstance(deliver_sites, list):
        deliver_sites = []
    return json.dumps([str(c) for c in deliver_sites if isinstance(c, str)])


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["deliver_sites"] = json.loads(d.get("deliver_sites") or "[]")
    except (json.JSONDecodeError, ValueError, TypeError):
        d["deliver_sites"] = []
    return d


def create_commission(conn: sqlite3.Connection, *, client_name: str,
                      description: str = "", price: float = 0,
                      currency: str = "USD", status: str = "quote",
                      due_date: str = "", artwork_name: str = "",
                      deliver_sites=None, notes: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO commissions (client_name, description, price, currency, "
        "status, due_date, artwork_name, deliver_sites, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (client_name or "Unnamed client", description or "", float(price or 0),
         currency or "USD", status if status in STATUSES else "quote",
         due_date or "", artwork_name or "", _clean_sites(deliver_sites), notes or ""))
    return cur.lastrowid


def list_commissions(conn: sqlite3.Connection, archived: bool = False) -> list[dict]:
    """Active commissions by default; pass archived=True for the archived pile."""
    return [_row(r) for r in conn.execute(
        "SELECT * FROM commissions WHERE archived = ? ORDER BY "
        # Undated rows sink to the bottom; otherwise soonest-due first.
        "CASE WHEN due_date = '' THEN 1 ELSE 0 END, due_date ASC, id DESC",
        (1 if archived else 0,)).fetchall()]


def count_archived(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM commissions WHERE archived = 1").fetchone()[0]


def set_archived(conn: sqlite3.Connection, cid: int, archived: bool) -> None:
    conn.execute(
        "UPDATE commissions SET archived = ?, updated_at = datetime('now') WHERE id = ?",
        (1 if archived else 0, cid))


def get_commission(conn: sqlite3.Connection, cid: int) -> dict | None:
    row = conn.execute("SELECT * FROM commissions WHERE id = ?", (cid,)).fetchone()
    return _row(row) if row else None


def update_commission(conn: sqlite3.Connection, cid: int, **fields) -> None:
    allowed = {"client_name", "description", "price", "currency", "status",
               "due_date", "artwork_name", "deliver_sites", "notes", "archived"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "deliver_sites":
            v = _clean_sites(v)
        elif k == "price":
            v = float(v or 0)
        elif k == "archived":
            v = 1 if v else 0
        elif k == "status" and v not in STATUSES:
            continue
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    params.append(cid)
    conn.execute(f"UPDATE commissions SET {', '.join(sets)} WHERE id = ?", params)


def delete_commission(conn: sqlite3.Connection, cid: int) -> None:
    conn.execute("DELETE FROM commissions WHERE id = ?", (cid,))
