"""GIF → MP4 for sites that only move video (spec 030, Threads).

Threads takes JPEG/PNG pictures and MP4/MOV video; a GIF sent as a picture would freeze. The bundled ffmpeg
(``imageio-ffmpeg``, a static binary) re-encodes it as H.264 at 30 fps (Threads wants 23–60) with even dimensions
(yuv420p needs them) and no sound track. Threads loops short videos, so the GIF isn't repeated.

**Licence isolation**: the binary is GPL-3.0 and is only ever *run* as a separate program here — never imported
into or linked with PawPoller (AGPL-3.0), the same arrangement as gallery-dl.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

NO_ENCODER = "This copy of PawPoller can't turn GIFs into video (its video encoder is missing)"
_FILTER = "fps=30,scale='min(1920,iw)':-2:flags=lanczos,pad=ceil(iw/2)*2:ceil(ih/2)*2"


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def ffmpeg_available() -> bool:
    return bool(_ffmpeg())


async def gif_to_mp4(path: str) -> str:
    """Encode *path* as a looping-friendly MP4; return the temp file's path (the caller deletes it)."""
    exe = _ffmpeg()
    if not exe:
        raise RuntimeError(NO_ENCODER)
    fd, out = tempfile.mkstemp(suffix=".mp4", prefix="pp_gif_")
    os.close(fd)
    cmd = [exe, "-y", "-loglevel", "error", "-i", path, "-an", "-movflags", "+faststart", "-pix_fmt", "yuv420p",
           "-c:v", "libx264", "-crf", "20", "-vf", _FILTER, out]
    try:
        r = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=120,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        os.remove(out)
        raise RuntimeError("Turning the GIF into video took too long (over 2 minutes)")
    if r.returncode != 0 or not os.path.getsize(out):
        os.remove(out)
        logger.error("GIF → MP4 failed: %s", (r.stderr or b"").decode(errors="replace")[-500:])
        raise RuntimeError("Couldn't turn the GIF into video")
    return out
