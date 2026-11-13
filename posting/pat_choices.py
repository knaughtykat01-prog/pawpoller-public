"""A piece's Patreon choices → the files a post sends (spec 084, data-model "A post job's Patreon choices").

The choices live on the piece (artwork ``categories["pat"]``, story ``platform_options["pat"]``), the way Toyhouse's
characters do, so a scheduled post reads them when it sends. Two rules keep a form value from becoming a file path
(plan path row, security review Medium 2):

- only the keys in ``CHOICE_KEYS`` are read from the piece; anything else saved there is dropped;
- the form names things by key (``cover: main`` / ``variant:<key>`` / ``story``, ``variant_keys``, ``files``), and
  the resolved paths travel in the package's own ``pat_files`` field, which the reader always sets. The poster checks
  each path with ``inside()`` against ``pat_files["piece_dir"]`` again before uploading.

Every picture and the cover carries its own rating, so the poster can refuse one rated above the post. "Attach
PDF/EPUB" is the whole story's file only (``PDF/*.pdf``, ``EPUB/*.epub``), never a chapter's own: more than one match
is refused, so an unreleased chapter can't ride along (security review Medium 3).
"""
from __future__ import annotations

from pathlib import Path

CHOICE_KEYS = ("audience", "tier_ids", "notify", "cover", "variant_keys", "files", "collection",
               "picture_place", "caption", "publish_at", "public_at")     # the last four: FR-076 to FR-079
STORY_FILES = {"pdf": ("PDF", "*.pdf"), "epub": ("EPUB", "*.epub")}
MAX_PICTURES = 20


def pick(opts) -> dict:
    """The Patreon keys (``CHOICE_KEYS``) from a piece's saved options, nothing else."""
    return {k: opts[k] for k in CHOICE_KEYS if isinstance(opts, dict) and k in opts}


def _keys(value) -> list[str]:
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []


def for_artwork(artwork, image_path: str | None, rating: str, opts: dict) -> dict:
    """Resolve an art post's pictures and cover. ``image_path`` / ``rating`` are the render actually being sent
    (the substituted one when an Adult piece sends a lower-rated render), so ``cover: main`` is that render."""
    from posting.artwork_reader import variant_image_path
    out = {"piece_dir": str(getattr(artwork, "path", "") or ""), "pictures": [], "cover": None, "files": [],
           "problems": []}
    by_key = {v.get("key"): v for v in (getattr(artwork, "variants", None) or []) if isinstance(v, dict) and v.get("key")}

    def render(key: str):
        v = by_key.get(key)
        if v is None:
            out["problems"].append(f"This piece has no picture called {key!r} for Patreon.")
            return None
        p = variant_image_path(artwork, v)
        if p is None:
            out["problems"].append(f"The {v.get('label') or key!r} picture is missing from the piece's folder.")
            return None
        return (str(p), v.get("rating") or getattr(artwork, "rating", "") or "")

    if image_path:
        out["pictures"].append((str(image_path), rating))
    for key in _keys(opts.get("variant_keys")):
        got = render(key)
        if got and got[0] not in {p for p, _ in out["pictures"]}:
            out["pictures"].append(got)
    if len(out["pictures"]) > MAX_PICTURES:
        out["problems"].append(f"Patreon posts from PawPoller take up to {MAX_PICTURES} pictures.")
    cover = str(opts.get("cover") or "none").strip()
    if cover == "main":
        out["cover"] = (str(image_path), rating) if image_path else None
    elif cover.startswith("variant:"):
        out["cover"] = render(cover[len("variant:"):])
    elif cover not in ("", "none"):
        out["problems"].append("Pick a cover from this piece's own pictures for Patreon.")
    return out


def for_story(story, thumbnail: str | None, rating: str, opts: dict) -> dict:
    """Resolve a chapter post's cover (the story's) and story files (the whole story's PDF / EPUB)."""
    from posting.artwork_reader import inside
    root = Path(str(getattr(story, "path", "") or ""))
    out = {"piece_dir": str(root), "pictures": [], "cover": None, "files": [], "problems": []}
    cover = str(opts.get("cover") or "none").strip()
    if cover == "story":
        p = inside(root, thumbnail) if thumbnail else None
        if p is None or not p.is_file():
            out["problems"].append("This story has no cover picture to use on Patreon.")
        else:
            out["cover"] = (str(p), rating)
    elif cover not in ("", "none"):
        out["problems"].append("A chapter's Patreon cover can only be the story's own cover.")
    for fmt in _keys(opts.get("files")):
        spec = STORY_FILES.get(fmt.lower())
        if spec is None:
            out["problems"].append(f"PawPoller can't attach {fmt!r} files to Patreon (PDF or EPUB only).")
            continue
        folder, pattern = spec
        found = sorted(f for f in (root / folder).glob(pattern) if f.is_file()) if (root / folder).is_dir() else []
        if not found:
            out["problems"].append(f"This story has no {fmt.upper()} file to attach.")
        elif len(found) > 1:
            out["problems"].append(f"This story has more than one {fmt.upper()} file ({', '.join(f.name for f in found)}); "
                                   "keep only the whole story's.")
        else:
            p = inside(root, f"{folder}/{found[0].name}")
            if p is None:
                out["problems"].append(f"The story's {fmt.upper()} file is outside its folder.")
            else:
                out["files"].append(str(p))
    return out
