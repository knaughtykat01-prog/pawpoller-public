"""Slop score computation for the story editor.

Ported from m_x/Scripts_Utils/slop_scorer.py. Loads the EQ-Bench word
and trigram lists once at import time and exposes a score_text() function
for in-memory scoring without file I/O.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data loading (one-time at import)
# ---------------------------------------------------------------------------

_SLOP_WORDS: set[str] = set()
_SLOP_TRIGRAMS: set[str] = set()
_LOADED = False


def is_available() -> bool:
    """True when the slop word/trigram data files were loaded successfully."""
    _ensure_loaded()
    return bool(_SLOP_WORDS and _SLOP_TRIGRAMS)


def _ensure_loaded():
    global _SLOP_WORDS, _SLOP_TRIGRAMS, _LOADED
    if _LOADED:
        return

    # Look for data files in Scripts_Utils. Local desktop wins via the
    # m_x/ sibling so user edits to the canonical files take effect; the
    # bundled PawPoller/scripts_utils/ copy is the fallback used by the
    # Docker container (which doesn't mount m_x/).
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "m_x" / "Scripts_Utils",
        Path(__file__).resolve().parent.parent / "scripts_utils",
        Path(os.environ.get("SLOP_DATA_DIR", "")),
    ]
    for base in candidates:
        words_path = base / "slop_words.json"
        trigrams_path = base / "slop_trigrams.json"
        if words_path.is_file() and trigrams_path.is_file():
            with open(words_path, encoding="utf-8") as f:
                raw_words = json.load(f)
            with open(trigrams_path, encoding="utf-8") as f:
                raw_trigrams = json.load(f)

            for entry in raw_words:
                if isinstance(entry, list):
                    for w in entry:
                        _SLOP_WORDS.add(w.lower().strip())
                else:
                    _SLOP_WORDS.add(str(entry).lower().strip())

            for entry in raw_trigrams:
                if isinstance(entry, list):
                    for t in entry:
                        _SLOP_TRIGRAMS.add(t.lower().strip())
                else:
                    _SLOP_TRIGRAMS.add(str(entry).lower().strip())

            _LOADED = True
            logger.info(
                "Slop scorer loaded %d words + %d trigrams from %s",
                len(_SLOP_WORDS), len(_SLOP_TRIGRAMS), base,
            )
            return

    # Data files not found at any candidate path — scoring will return
    # 0.0 with empty hit dicts. Frontend reads is_available() to render
    # an "unavailable" state instead of a misleading clean score.
    logger.warning(
        "Slop scorer data files not found; scoring disabled. Searched: %s",
        ", ".join(str(p) for p in candidates if str(p)),
    )
    _LOADED = True  # don't retry


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class SlopResult:
    score: float = 0.0
    word_count: int = 0
    word_hits: dict[str, int] = field(default_factory=dict)
    trigram_hits: dict[str, int] = field(default_factory=dict)
    contrast_count: int = 0
    rating: str = "UNKNOWN"  # CLEAN, BORDERLINE, SLOP


def _clean_text(text: str) -> str:
    text = re.sub(r"[#*_`~\[\]\(\)]", "", text)
    text = re.sub(r"---+", "", text)
    # Strip HTML comment anchors
    text = re.sub(r"<!--.*?-->", "", text)
    return text


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z']+", text.lower())


def score_text(text: str) -> SlopResult:
    """Score markdown text for AI-typical word patterns.

    Returns a SlopResult with score (0-100), hit details, and rating.
    Score < 15 = CLEAN, 15-25 = BORDERLINE, > 25 = SLOP.
    """
    _ensure_loaded()

    result = SlopResult()
    clean = _clean_text(text)
    tokens = _tokenize(clean)
    result.word_count = len(tokens)

    if result.word_count == 0:
        result.rating = "CLEAN"
        return result

    # Word hits
    for t in tokens:
        if t in _SLOP_WORDS:
            result.word_hits[t] = result.word_hits.get(t, 0) + 1

    total_word_hits = sum(result.word_hits.values())
    words_per_1k = (total_word_hits / result.word_count) * 1000

    # Trigram hits
    for i in range(len(tokens) - 2):
        tri = f"{tokens[i]} {tokens[i+1]} {tokens[i+2]}"
        if tri in _SLOP_TRIGRAMS:
            result.trigram_hits[tri] = result.trigram_hits.get(tri, 0) + 1

    total_trigram_hits = sum(result.trigram_hits.values())
    trigrams_per_1k = (total_trigram_hits / result.word_count) * 1000

    # Contrast patterns ("Not X. Y." / "Not X, but Y")
    patterns = [
        r"not\s+\w+[\s,;\u2014-]+but\s",
        r"not\s+just\s+\w+[\s,;\u2014-]+but\s",
        r"isn't\s+\w+[\s,;\u2014-]+it's\s",
    ]
    lower = clean.lower()
    for p in patterns:
        result.contrast_count += len(re.findall(p, lower))
    contrast_per_1k = (result.contrast_count / result.word_count) * 1000

    # Score formula (EQ-Bench methodology)
    norm_words = min(words_per_1k / 20.0, 1.0)
    norm_contrast = min(contrast_per_1k / 5.0, 1.0)
    norm_trigrams = min(trigrams_per_1k / 10.0, 1.0)
    result.score = round((norm_words * 0.6 + norm_contrast * 0.25 + norm_trigrams * 0.15) * 100, 1)

    if result.score < 15:
        result.rating = "CLEAN"
    elif result.score < 25:
        result.rating = "BORDERLINE"
    else:
        result.rating = "SLOP"

    return result
