"""Trello inside PawPoller — a full, two-way board mirror.

Specs: `specs/006-trello-board-mirror/` (current), `specs/005-trello-commissions/`
(the commission-only sync it replaced; its link table and card block survive).

Layout:

* ``mirror``     — apply a Trello read to the local copy, field by field, against
  the ``agreed`` value. No network.
* ``outbox``     — PawPoller's edits, queued with the mirror write and sent in order.
* ``positions``  — where a dropped card/list lands in Trello's ``pos`` terms.
* ``views``      — read-only shapes for the API (every read is local).
* ``commission`` — a card marked as a commission; spec 005 → 006 migration.
* ``covers``     — cover images kept on disk.
* ``webhooks``   — registration and turning a delivery into "re-read this board".
* ``runtime``    — in-memory state shared by the two threads and the routes.
* ``mapping``    — settings, credentials, ownership, list → status.
* ``card_block`` — spec 005's description block; now only stripped, by the migration.

⚠ The invariant: ``agreed`` advances only after Trello accepted the write that
needed it. An interrupted send leaves the change looking pending — which it is.
"""
