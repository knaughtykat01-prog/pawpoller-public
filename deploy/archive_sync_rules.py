"""What the story archive does and does not carry — mirroring Stage 4.

The story archive is synced by two scripts pointing in opposite directions:
``pawsync.py`` pushes local → server, ``pawpull.py`` pulls server → local. Until
now each carried **its own** exclude list, and they disagreed:

    pawsync (up)    Backups, Drafts, Styled_HTML
    pawpull (down)  Backups, Drafts, Chapters_backup_*

Neither list is wrong on its own. Together they mean **a push followed by a pull
does not return the archive to where it started**, which is the property a sync
pair exists to have:

* ``Chapters_backup_*`` is excluded coming down but not going up, so a local
  backup directory is uploaded once and then lives on the server forever — the
  pull that would balance it refuses to look, and ``ssh_prune`` only walks the
  top level, so nothing ever removes it.
* ``Styled_HTML`` is excluded going up but not coming down, so anything of that
  name that reaches the server is pulled into every desktop afterwards and can
  never be pushed back — the divergence is one-way and permanent.
* ``*.bak.<unix-ts>`` was excluded by **neither**, which is where the spec's
  "the server has 3 extra .bak files" came from. The artwork mirror has always
  dropped these (``mirror/core.py``'s ``_BAK_RE``); the story archive kept
  syncing them, so the same class of file was canonical in one store and
  derived in the other.

``pawpull``'s own comment said it excluded ``Styled_HTML``. It did not. That is
the whole failure in one line: two lists drift, and the comment drifts with
them.

**So the rules live here, once, and both directions import them.** The
asymmetry cannot come back without someone editing this file, at which point
they are editing both directions at the same time and can see it.

## What each rule is, and why it is safe to drop

Everything excluded here is **derived or per-device** — reproducible from the
canonical files, or meaningful only on the machine that made it. Per §4 of the
spec the canonical set is ``MASTER.md`` + ``story.json`` + covers + tags;
``HTML/``, ``BBCode/``, ``PDF/``, ``EPUB/`` and ``Chapters/`` are derived but
**do** travel, because the posters upload ``SquidgeWorld/*.html`` and
``Chapters/SoFurry_HTML/*.html`` directly and the server has no browser to
regenerate a PDF (``pdf_generator.py``). Derived-but-needed is still carried;
only derived-and-unused is dropped.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

# Directory names dropped wherever they appear in the path.
EXCLUDE_DIR_NAMES: tuple[str, ...] = (
    # Per-story undo history written by the editor. Timestamp-named and pruned
    # locally, so syncing it churns and never converges.
    "Backups",
    # Work in progress. The archive is the *complete* stories; a draft on one
    # machine is not a fact the other needs.
    "Drafts",
    # Local styling artefacts. Not read by any poster — the posters want
    # `SquidgeWorld/` and `Chapters/SoFurry_HTML/`, which are not these.
    "Styled_HTML",
)

# Glob patterns matched against any single path component.
EXCLUDE_GLOBS: tuple[str, ...] = (
    # Snapshot of a Chapters/ directory taken before a re-split. Regenerable
    # from MASTER.md and only interesting on the machine that split.
    "Chapters_backup_*",
)

# `*.bak.<unix-ts>` — the undo file written beside anything the app rewrites.
# Same rule the artwork mirror has always applied (`mirror/core.py:_BAK_RE`);
# applying it here makes one class of file mean one thing across both stores.
BAK_RE = re.compile(r"\.bak\.\d+$")


def is_excluded(rel_path: str) -> bool:
    """True if this archive-relative path must not cross in either direction.

    Takes the path relative to the archive root, with either separator — the
    two callers produce different ones (``pathlib`` on Windows, ``tar`` on the
    server), and normalising here is cheaper than remembering to at each site.
    """
    parts = PurePosixPath(str(rel_path).replace("\\", "/")).parts
    for part in parts:
        if part in EXCLUDE_DIR_NAMES:
            return True
        if BAK_RE.search(part):
            return True
        for pattern in EXCLUDE_GLOBS:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


def tar_exclude_flags() -> str:
    """The same rules as GNU ``tar --exclude`` flags, for the remote pack.

    ``pawpull`` packs on the server by shelling to ``tar`` there (Linux, so the
    Windows ``tar`` trap in §5.3 does not apply — that one is about invoking
    ``tar`` *locally* with a ``C:\\…`` path). Rendering the flags from the same
    tuples is what stops the remote command drifting from the local predicate.

    ``tar``'s ``--exclude`` matches a glob against the whole member path, so a
    bare directory name needs wrapping to match at any depth.
    """
    flags = []
    for name in EXCLUDE_DIR_NAMES:
        flags.append(f"--exclude='{name}'")
        flags.append(f"--exclude='*/{name}'")
        flags.append(f"--exclude='*/{name}/*'")
    for pattern in EXCLUDE_GLOBS:
        flags.append(f"--exclude='{pattern}'")
        flags.append(f"--exclude='*/{pattern}'")
        flags.append(f"--exclude='*/{pattern}/*'")
    # tar has no regex, so the .bak rule is spelled as the glob it really is.
    flags.append("--exclude='*.bak.[0-9]*'")
    return " ".join(flags)


def classify(local: dict, server: dict) -> dict:
    """Compare two ``{relative_path: size}`` maps and say how they differ.

    Deliberately reports rather than resolves. Which side wins for a
    genuinely-divergent file is a data decision the spec assigns to a human
    (§7.2), and the standing no-AI rule means nothing here may guess: the job
    is to produce the list, not an opinion about it.

    ``excluded_present`` is the interesting one for Stage 4 — files that the
    rules say should never have crossed but which exist on a side anyway. They
    are the residue of the old asymmetry, and they are what a reconciliation
    has to decide about.
    """
    local_kept = {p: s for p, s in local.items() if not is_excluded(p)}
    server_kept = {p: s for p, s in server.items() if not is_excluded(p)}

    local_only = sorted(set(local_kept) - set(server_kept))
    server_only = sorted(set(server_kept) - set(local_kept))
    differing = sorted(p for p in set(local_kept) & set(server_kept)
                       if local_kept[p] != server_kept[p])
    identical = sorted(p for p in set(local_kept) & set(server_kept)
                       if local_kept[p] == server_kept[p])

    return {
        "local_only": local_only,
        "server_only": server_only,
        "size_differs": differing,
        "identical": len(identical),
        "excluded_present": {
            "local": sorted(p for p in local if is_excluded(p)),
            "server": sorted(p for p in server if is_excluded(p)),
        },
        # The property Stage 4 exists to restore: with one shared rule set, a
        # push then a pull leaves nothing behind on either side.
        "round_trip_clean": not (local_only or server_only or differing),
    }


def story_of(rel_path: str) -> str:
    """The top-level story folder a path belongs to, for grouping a report."""
    parts = PurePosixPath(str(rel_path).replace("\\", "/")).parts
    return parts[0] if parts else ""
