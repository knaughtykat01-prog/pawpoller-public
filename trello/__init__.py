"""Two-way Trello sync for commissions.

Spec: `specs/005-trello-commissions/`.

The operator works commissions from a phone, on a Trello board; PawPoller is where
they are recorded next to the artwork they produced. This package keeps the two in
step in both directions.

Layout, and why it is split this way:

* ``card_block``  — the delimited block appended to a card's description. Trello
  cards have no price field, so price, currency and the correlation key live
  there. Render / parse / **strip**.
* ``diff``        — the three-way engine. Pure: two dictionaries and a baseline in,
  a decision per field out. No database, no network, no clock. Every guarantee
  about not losing an edit is enforced here and tested exhaustively.
* ``mapping``     — the board and the status↔column map, read from settings.
* ``sync``        — orchestration. Reads the board once, asks ``diff``, applies,
  advances the baseline, reports.

⚠ The one invariant worth stating at the top: a field's baseline is advanced
**after** the write that field required succeeded, never before and never for the
batch. That is what makes an interrupted sync harmless — an unwritten field still
looks like a pending change, which is exactly what it is.
"""
