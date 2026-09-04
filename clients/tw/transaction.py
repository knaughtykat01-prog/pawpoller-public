"""X's per-request ``x-client-transaction-id`` — the header its own web client
signs every request with, and the one PawPoller never sent (4.6.2).

Why this exists: X refuses PawPoller's ``CreateTweet`` with error 226 ("this
request looks like it might be automated") while the same cookie session reads
the timeline fine. The 4.3.5 write-path headers did not clear it — two 226s
two minutes apart on 2026-09-04, identical. What the browser sends and we did
not is this header, so this is the experiment: writes carry it, reads are left
exactly as they were (polling works and is not being risked).

How the id is built (X's own scheme, reverse-engineered by others, see below):

  1. the home page carries ``<meta name="twitter-site-verification"
     content="<base64 key>">`` — the KEY, ~48 bytes;
  2. the page also carries four SVGs ``id="loading-x-anim-0..3"``; one of them
     (chosen by a key byte) has a path whose ``d`` attribute is a 2D table of
     numbers — the ANIMATION FRAMES;
  3. the ``ondemand.s.<hash>a.js`` bundle (its hash is in the page's script
     manifest) contains the INDICES of the key bytes that select a frame row and
     a frame time;
  4. an "animation key" is computed by running one frame through a cubic-bezier
     easing at that time (a colour + a rotation matrix, hex-encoded);
  5. per request: ``sha256("<METHOD>!<path>!<seconds since 2023-05-01>obfiowerehiring<animation key>")``,
     then ``[key bytes, 4 time bytes, 16 hash bytes, 3]`` XORed with one random
     byte, prefixed by that byte, base64 without padding.

Everything above is X's private protocol: it has a shelf life, and when X
rotates the scheme this module needs updating and X posting pauses until then.
That is the deal that was chosen over the official API (BACKLOG TWTXID /
TWAUTO). Failure here is soft: no header, the post goes out as before.

Ported from XClientTransaction (https://github.com/iSarabjitDhiman/XClientTransaction,
itself from TweeterPy), MIT License, Copyright (c) Sarabjit Dhiman:
  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions: The above copyright
  notice and this permission notice shall be included in all copies or
  substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS",
  WITHOUT WARRANTY OF ANY KIND.
The port drops the BeautifulSoup dependency (the three things it needs from the
page are found with regular expressions) and keeps the arithmetic verbatim —
it was checked against the reference implementation on the live page.

No model is involved: this is parsing and arithmetic.
"""
from __future__ import annotations

import base64
import hashlib
import math
import random
import re
import time
from functools import reduce

ADDITIONAL_RANDOM_NUMBER = 3
DEFAULT_KEYWORD = "obfiowerehiring"
# Seconds are counted from 2023-05-01T07:00:00Z in X's client.
_EPOCH = 1682924400

ON_DEMAND_FILE_URL = "https://abs.twimg.com/responsive-web/client-web/ondemand.s.{filename}a.js"
_ON_DEMAND_INDEX = re.compile(r""",(\d+):["']ondemand\.s["']""")
_ON_DEMAND_HASH = r',{}:"([0-9a-f]+)"'
_INDICES = re.compile(r"""(\(\w{1}\[(\d{1,2})\],\s*16\))+""")
_MIGRATION_URL = re.compile(
    r"""(http(?:s)?://(?:www\.)?(twitter|x){1}\.com(/x)?/migrate([/?])?tok=[a-zA-Z0-9%\-_]+)+""")
# <meta name="twitter-site-verification" content="…"> — attribute order varies.
_META_KEY = (
    re.compile(r"""<meta\b[^>]*\bname=["']twitter-site-verification["'][^>]*\bcontent=["']([^"']+)["']""", re.I),
    re.compile(r"""<meta\b[^>]*\bcontent=["']([^"']+)["'][^>]*\bname=["']twitter-site-verification["']""", re.I),
)
_SVG_FRAME = re.compile(r"""<svg\b[^>]*\bid=["']loading-x-anim-(\d)["'][^>]*>(.*?)</svg>""", re.S | re.I)
_PATH_D = re.compile(r"""<path\b[^>]*\sd=["']([^"']+)["']""", re.I)


class TransactionError(Exception):
    """The page or bundle no longer looks the way this module expects —
    X changed something. The caller posts without the header and logs it."""


# ── page parsing (regex where the reference used bs4) ────────────────────

def ondemand_file_url(home_html: str) -> str:
    m = _ON_DEMAND_INDEX.search(home_html)
    if not m:
        raise TransactionError("ondemand.s index not found in the home page")
    h = re.search(_ON_DEMAND_HASH.format(m.group(1)), home_html)
    if not h:
        raise TransactionError("ondemand.s hash not found in the home page")
    return ON_DEMAND_FILE_URL.format(filename=h.group(1))


def migration_url(home_html: str) -> str:
    """The twitter.com → x.com migration hop, when the page is that instead."""
    m = _MIGRATION_URL.search(home_html)
    return m.group(0) if m else ""


def verification_key(home_html: str) -> str:
    for rx in _META_KEY:
        m = rx.search(home_html)
        if m:
            return m.group(1)
    raise TransactionError("twitter-site-verification key not found in the home page")


def key_bytes(key: str) -> list[int]:
    return list(base64.b64decode(key.encode("utf-8")))


def indices(ondemand_js: str) -> tuple[int, list[int]]:
    found = [int(m.group(2)) for m in _INDICES.finditer(ondemand_js)]
    if not found:
        raise TransactionError("key-byte indices not found in ondemand.s")
    return found[0], found[1:]


def frames_2d(home_html: str, kbytes: list[int]) -> list[list[int]]:
    """The animation table: the SVG picked by ``key[5] % 4``, its second
    ``<path d="…">``, ``d[9:]`` split on ``C``, each piece's integers."""
    svgs = {int(m.group(1)): m.group(2) for m in _SVG_FRAME.finditer(home_html)}
    if not svgs:
        raise TransactionError("loading-x-anim SVGs not found in the home page")
    which = kbytes[5] % 4
    if which not in svgs:
        raise TransactionError(f"loading-x-anim-{which} not found in the home page")
    paths = _PATH_D.findall(svgs[which])
    if len(paths) < 2:
        raise TransactionError("animation path not found in the SVG")
    d = paths[1]
    return [[int(x) for x in re.sub(r"[^\d]+", " ", item).strip().split()]
            for item in d[9:].split("C")]


# ── the arithmetic, verbatim from the reference ─────────────────────────

def _js_round(num: float) -> float:
    x = math.floor(num)
    if (num - x) >= 0.5:
        x = math.ceil(num)
    return math.copysign(x, num)


def _float_to_hex(x: float) -> str:
    result: list[str] = []
    quotient = int(x)
    fraction = x - quotient
    while quotient > 0:
        quotient = int(x / 16)
        remainder = int(x - (float(quotient) * 16))
        result.insert(0, chr(remainder + 55) if remainder > 9 else str(remainder))
        x = float(quotient)
    if fraction == 0:
        return "".join(result)
    result.append(".")
    while fraction > 0:
        fraction *= 16
        integer = int(fraction)
        fraction -= float(integer)
        result.append(chr(integer + 55) if integer > 9 else str(integer))
    return "".join(result)


def _is_odd(num: int) -> float:
    return -1.0 if num % 2 else 0.0


class _Cubic:
    def __init__(self, curves: list[float]):
        self.curves = curves

    @staticmethod
    def _calc(a: float, b: float, m: float) -> float:
        return 3.0 * a * (1 - m) * (1 - m) * m + 3.0 * b * (1 - m) * m * m + m * m * m

    def value(self, t: float) -> float:
        c = self.curves
        start, mid, end = 0.0, 0.0, 1.0
        if t <= 0.0:
            g = 0.0
            if c[0] > 0.0:
                g = c[1] / c[0]
            elif c[1] == 0.0 and c[2] > 0.0:
                g = c[3] / c[2]
            return g * t
        if t >= 1.0:
            g = 0.0
            if c[2] < 1.0:
                g = (c[3] - 1.0) / (c[2] - 1.0)
            elif c[2] == 1.0 and c[0] < 1.0:
                g = (c[1] - 1.0) / (c[0] - 1.0)
            return 1.0 + g * (t - 1.0)
        while start < end:
            mid = (start + end) / 2
            x_est = self._calc(c[0], c[2], mid)
            if abs(t - x_est) < 0.00001:
                return self._calc(c[1], c[3], mid)
            if x_est < t:
                start = mid
            else:
                end = mid
        return self._calc(c[1], c[3], mid)


def _interpolate(a: list[float], b: list[float], f: float) -> list[float]:
    if len(a) != len(b):
        raise TransactionError("mismatched interpolation arguments")
    return [x * (1 - f) + y * f for x, y in zip(a, b)]


def _rotation_matrix(deg: float) -> list[float]:
    rad = math.radians(deg)
    return [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]


def _solve(value: float, lo: float, hi: float, rounding: bool) -> float:
    result = value * (hi - lo) / 255 + lo
    return math.floor(result) if rounding else round(result, 2)


def _animate(frames: list[int], target_time: float) -> str:
    from_color = [float(v) for v in [*frames[:3], 1]]
    to_color = [float(v) for v in [*frames[3:6], 1]]
    from_rot = [0.0]
    to_rot = [_solve(float(frames[6]), 60.0, 360.0, True)]
    rest = frames[7:]
    curves = [_solve(float(v), _is_odd(i), 1.0, False) for i, v in enumerate(rest)]
    val = _Cubic(curves).value(target_time)
    color = [max(0, min(255, v)) for v in _interpolate(from_color, to_color, val)]
    rotation = _interpolate(from_rot, to_rot, val)
    matrix = _rotation_matrix(rotation[0])
    out = [format(round(v), "x") for v in color[:-1]]
    for v in matrix:
        r = round(v, 2)
        if r < 0:
            r = -r
        hx = _float_to_hex(r)
        out.append(f"0{hx}".lower() if hx.startswith(".") else (hx if hx else "0"))
    out.extend(["0", "0"])
    return re.sub(r"[.-]", "", "".join(out))


def animation_key(kbytes: list[int], table: list[list[int]], row_index_at: int,
                  time_indices: list[int]) -> str:
    total_time = 4096
    row = kbytes[row_index_at] % 16
    frame_time = reduce(lambda a, b: a * b, [kbytes[i] % 16 for i in time_indices])
    frame_time = _js_round(frame_time / 10) * 10
    if row >= len(table):
        raise TransactionError("animation table has too few rows")
    return _animate(table[row], float(frame_time) / total_time)


# ── the object the client keeps ─────────────────────────────────────────

class ClientTransaction:
    """Built once from the home page + ondemand.s; ``generate`` per request."""

    def __init__(self, home_html: str, ondemand_js: str):
        self.row_index_at, self.time_indices = indices(ondemand_js)
        self.key = verification_key(home_html)
        self.key_bytes = key_bytes(self.key)
        self.animation_key = animation_key(self.key_bytes, frames_2d(home_html, self.key_bytes),
                                           self.row_index_at, self.time_indices)
        self.built_at = time.time()

    def generate(self, method: str, path: str, *, time_now: int | None = None,
                 random_byte: int | None = None) -> str:
        """The header value for one request. ``path`` is the URL path only."""
        time_now = time_now if time_now is not None else math.floor(time.time() - _EPOCH)
        time_bytes = [(time_now >> (i * 8)) & 0xFF for i in range(4)]
        digest = hashlib.sha256(
            f"{method}!{path}!{time_now}{DEFAULT_KEYWORD}{self.animation_key}".encode()).digest()
        rnd = random.randint(0, 255) if random_byte is None else random_byte
        payload = [*self.key_bytes, *time_bytes, *list(digest)[:16], ADDITIONAL_RANDOM_NUMBER]
        out = bytearray([rnd, *[b ^ rnd for b in payload]])
        return base64.b64encode(bytes(out)).decode().strip("=")
