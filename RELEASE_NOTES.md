> **PawPoller 4.0.** Every tool like this stops the moment your work is uploaded. This release
> is about the part that comes next: all your numbers in one place, and the ability to go back
> and change what you already published.
>
> **Edit once, sync everywhere.** Fix a title, description, tag or rating on a piece and push
> the correction out to every site that accepts one — **nine of them now**, DeviantArt
> descriptions included. Sites that genuinely cannot be edited are labelled post-only instead
> of being quietly skipped, so you can always see where a change landed and where it did not.
>
> **Art is a first-class citizen.** Artwork sits alongside writing everywhere it matters —
> the same publishing, the same analytics, the same editing.

The 4.0 milestone is not one feature. It is the point at which "publish everywhere" stopped
being the whole product and became the first half of it.

**Editing, end to end.** 3.x shipped editing for the platforms where it had always been
possible but never wired up, and in three cases the blocker was an unchecked assumption rather
than a real limit:

| Platform | Was | Is |
|---|---|---|
| e621 (3.33.0) | "uses a separate tag-edit API" | `PATCH /posts/{id}.json`, same credentials as polling |
| Itaku (3.35.0) | "does not support editing via API" | `PATCH /api/galleries/images/{id}/`, the sibling of its own upload |
| DeviantArt (3.36.0) | "no image-edit endpoint exists" | `POST /deviation/edit/{id}`, plus the editor's own endpoint for the description |

Each of those was a comment nobody had tested. The lesson is recorded in the per-platform
docstrings, because the same sentence is still sitting in front of FurryNetwork.

**Nine editable platforms:** Inkbunny, FurAffinity, SoFurry, Weasyl, AO3, SquidgeWorld,
DeviantArt, Itaku and e621. Each has its own rules and the code respects them rather than
flattening them — e621's tags **merge** because they are communal, Itaku's **replace** because
they are the owner's, e621 has **no title field at all**, and DeviantArt splits one edit across
two endpoints because neither carries the other's fields.

**Capability flags stopped overstating.** `supports_artwork_edit` exists because DeviantArt can
edit literature but needed a different endpoint for images, and one flag could not say so. A
poster that cannot do something now says which thing, and `update_artwork` skips it and writes
no publication row — rather than attempting it, failing, and recording that failure against a
live, correctly-posted submission.

**Twenty platforms, honestly counted:** 19 polled, 17 posted to, 9 editable. The README and the
marketing site were both re-derived from the poster registry rather than trusted; they had been
claiming FurAffinity posting was desktop-only (disproven in 3.26.0), that e621, Mastodon,
Tumblr and X could not be posted to (all four can), and Telegram was missing entirely.

No schema migration. No configuration change. Upgrading from 3.36.0 is a straight version bump.
