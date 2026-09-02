# PawPoller Reskin — Concept-Layer Build Plan

**Status:** ✅ **COMPLETE 2026-07-10** — all 5 slices shipped live (prod **2.77.0**):
**A Bookshelf 2.73.0 · B Modes/Brut 2.74.0 · C Laurels 2.75.0 · D Ledger 2.76.0 · E Health/Workbench
2.77.0.** Slice A was the reskin's live debut (merged `reskin`→`master`, retiring the branch); B–E were
built directly on `master`, each previewed locally then deployed. Per-slice detail is in `CHANGELOG.md`,
`docs/HANDOFF.md`, and `docs/documentation_guide.md`.

## Where we are
The **foundation** is done and matches the Vol. III synthesis' "house style":
- **Quill** warm-paper / sienna palette (theme-aware, default), across every screen.
- **Top-bar nav** as default + a **user-switchable side rail** (Settings → Appearance →
  Navigation). This is exactly your Den note: *"the return of the top bar nav… customisable
  for the user to have a choice."*
- Dropdowns now close correctly (2.72.1 bug-fix).

What's **not** built yet is the set of concept **layers** your notes are excited about — the
things that make it *yours*, not just a re-tinted version of the old app. This plan stages
them, using your own Vol. III decision grid + `storyboard.html` build order.

## Principles for this phase
1. **Keep the foundation.** House style (Quill + top/side nav) stays; concept screens layer on.
2. **Path A holds where it can.** Reuse all existing backend/logic + real endpoints
   (`/api/works`, submissions, snapshots, platform health). New work is UI + light
   aggregation, not a re-implemented engine.
3. **Preview per slice, deploy on your word.** Every slice lands on `reskin`, previewable
   locally; nothing goes to `pawpoller.syncopates.app` without your go.
4. **Each slice is a real increment** — versioned, with the three doc surfaces (CHANGELOG /
   HANDOFF / documentation_guide) per the project ritual.
5. **Explicitly out** (you agreed): **Signal** and **Cartographer** dropped; **Telemetry**
   charts stay *in moderation* (no Grafana wall).

---

## The slices

### Slice A — Bookshelf  ·  *Atelier*  ·  effort: **L**  ·  impact: highest
> Your note: *"the idea of a library makes total sense, cover speaking the truth like that.
> And the work detail in that form looks amazing… definitely integrate."*

- **Library home** — a cover-forward shelf of your works (the covers "speak the truth":
  completion + publish status read off the cover treatment / badges). Built on top of the
  existing Submissions hub + `/api/works`; the existing filter/type/persona controls stay.
- **Work-detail page (the one you loved)** — big cover, a **per-platform "published to"**
  stat list (views/faves per platform), and a **chapter list with per-chapter live-site
  counts** + "chapter incomplete" flags (e.g. Ch.4 never reached AO3). Reuses existing
  work/submission + snapshot data.
- **Open decision:** does Bookshelf *become the default home* (you open into your work), or
  sit as a peer **"Library"** destination beside Overview? Fits the "customisable" theme — we
  can let the landing screen be a user choice.
- **Ships as:** the new Library/work-detail screens; Overview untouched unless you want it
  demoted.

### Slice B — Modes pane  ·  *Brut + Console + nav*  ·  effort: **S**  ·  quick win
> Your notes: Brut *"wow just wow… could be its own mode"*; Console *"integrate into the
> headless mode for access to the docker."*

- One **Settings → Modes** surface that consolidates what already exists and is scattered:
  **nav position** (top/side, built), **theme** (built), and **display mode**
  (Default / **Brut** / **Terminal** — both already live). One clear place to "change it as
  you like."
- Polish **Brut** into a first-class selectable skin (it exists; make it deliberate).
- **Console → headless:** document/route the terminal aesthetic to the **headless/Docker**
  operator surface rather than the main dashboard (keeps it niche where it belongs).
- Smallest lift; ties off two of your notes immediately.

### Slice C — Laurels  ·  *Den*  ·  effort: **M**  ·  motivational
> Your note: *"I like some idea of gamification, earning medals or ribbons for achievements.
> Even account medals could be a really cool idea. Motivational perhaps. Big view milestones
> are a neat idea."*

- A **Laurels** view: real **medals / ribbons** (10k reads, 1k comments, first art, breakout
  piece), a **view-milestone tracker** with a progress bar to the next milestone
  ("Den of 130k"), a **writing/polling streak**, and **per-account levels / trophies** (your
  "account medals").
- Data: computed from existing metrics (view/fave/comment totals + snapshot history). One
  small aggregation layer; no schema change if we derive from what's already stored.
- **Open decision:** milestones counted from **all-time history** vs **from-now-on** (history
  may be partial for older works).

### Slice D — Ledger  ·  *Almanac*  ·  effort: **M**
> Your note: *"The idea of a timeline is a neat idea… For account and the works?"*

- A **dated timeline** scoped to (1) a **work's** history and (2) an **account's** history:
  polls, posts, milestones, lapsed/reconnected sessions on one dated spine with typed node
  markers. Reached from work-detail and account-detail (a tab, **not** the home — time-order
  buries "is everything ok right now").
- Data: from snapshot history + polling/post logs.

### Slice E — Health strip + Workbench  ·  *Observatory + Bento*  ·  effort: **M**
> Your notes: Observatory *"I like the platform health idea"*; Bento *"the ability for users
> to… adjust settings of graphs, size of components / widgets seems fun and interesting."*

- **Health strip (Observatory):** the 16-platform ok/expired/warn/unconfigured wall as a
  **compact strip** on the home (not the whole page). Reuses the existing PlatformHealth data.
- **Workbench (Bento):** grow the existing Overview widget board into a real **edit mode** —
  drag / resize / add-remove widgets and pick chart types, then save the layout. This is the
  "use the platform how they like it" idea, extended from what's already there.

---

## Recommended sequence
1. **A — Bookshelf** (biggest "it's yours"; the storyboard's designated first build)
2. **B — Modes pane** (quick win; consolidates Brut/Terminal/nav you already have)
3. **C — Laurels** (motivational hook)
4. **D — Ledger** (depth, once work + account detail pages exist from A)
5. **E — Health strip + Workbench** (polish the home + power-user customisation)

*(Reorderable — B is the fastest if you want an early visible payoff; A is the highest value.
Ledger (D) benefits from Bookshelf (A) landing first, since it hangs off work/account detail.)*

## Cross-cutting / already present
- **⌘K command palette**, **notification centre**, **account switcher** — exist; carried
  forward, restyled to Quill.
- **Customisable landing** — the nav is already user-switchable; the "which screen do I open
  into" choice (Overview vs Bookshelf) is the natural next customisation.

## Decisions (locked 2026-07-10)
1. **Sequence approved as-is:** A → B → C → D → E, starting with **Bookshelf**.
2. **Bookshelf = peer "Library" destination** beside Overview; Overview stays the default
   landing (lower risk, metrics-first home preserved). A user-choosable landing screen can
   come later as a customisation.
3. **Deploy after each slice** — once you've previewed a slice locally and you're happy, it
   ships to `pawpoller.syncopates.app`. (Supersedes the original "build everything then one
   deploy" rule for this phase.)
4. *Still open, deferred to Slice C:* Laurels milestones from **all-time history** vs
   **from-now-on**.
