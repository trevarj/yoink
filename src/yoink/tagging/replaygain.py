"""ReplayGain (R128) album normalization for the mutagen tagger path.

The beets backend gets this for free via its ``replaygain`` plugin (enabled in
``beets_tagger``); the mutagen backend places final files directly into the
library without a beets import, so we compute the gain ourselves here.

Non-destructive: we only write ``R128_TRACK_GAIN`` / ``R128_ALBUM_GAIN`` tags --
no audio data is re-encoded. Opus decoders apply R128 output gain natively, and
players with ReplayGain support honor the tags.

Loudness is measured with ffmpeg's ``ebur128`` filter (the same filter beets'
ffmpeg backend uses). The R128 reference level is -23 LUFS, so a track's gain in
dB is ``23.0 - loudness``. Album loudness is the mean of the per-track integrated
loudnesses (the standard EBU R128 album approximation). The opus R128 gain tag
is a Q7.8 fixed-point integer: ``round(256 * gain_db)``.

Everything here is best-effort: a missing ffmpeg, an unreadable file, or a parse
failure makes a function return ``None``/``False`` so the caller can skip
normalization without failing the track.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import mutagen

# R128 reference level (EBU R128 / opus output-gain convention).
_R128_REF_LUFS = -23.0
# Q7.8 fixed-point: 256 units per dB. Clamped to the signed 16-bit range stored
# in the opus header output gain.
_Q78_SCALE = 256.0
_Q78_MIN = -(1 << 15)
_Q78_MAX = (1 << 15) - 1

# Final ebur128 summary line, e.g. "    I:         -21.8 LUFS".
_INTEGRATED_RE = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.MULTILINE)

# ffmpeg needs the full file decoded to get an integrated-loudness reading; a
# generous per-track ceiling keeps one pathological file from stalling the album.
_MEASURE_TIMEOUT = 120


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def r128_q78(loudness_lufs: float, ref_lufs: float = _R128_REF_LUFS) -> int:
    """Convert a measured loudness to an opus R128 Q7.8 gain value.

    ``gain_db = ref - loudness`` (so -23 LUFS -> 0 dB -> 0), then scaled to
    Q7.8 and clamped to the signed 16-bit opus output-gain range.
    """
    gain_db = ref_lufs - loudness_lufs
    q78 = round(_Q78_SCALE * gain_db)
    return max(_Q78_MIN, min(_Q78_MAX, q78))


def album_loudness(loudnesses: list[float]) -> float:
    """Mean of per-track integrated loudnesses -- the EBU R128 album gain basis."""
    if not loudnesses:
        return _R128_REF_LUFS
    return sum(loudnesses) / len(loudnesses)


def measure_loudness(path: Path) -> float | None:
    """Integrated loudness (LUFS) of one file via ffmpeg ebur128, or None.

    None means "could not measure" (ffmpeg missing, decode failed, no parse) --
    callers must treat that as skip, never reject.
    """
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        return None
    cmd = [
        ffmpeg,
        "-nostats",
        "-hide_banner",
        "-i",
        str(path),
        "-af",
        "ebur128=metadata=1",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_MEASURE_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    # The integrated-loudness summary is written to stderr.
    m = _INTEGRATED_RE.search(proc.stderr or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def write_r128(path: Path, track_gain_q78: int, album_gain_q78: int) -> bool:
    """Write R128_TRACK_GAIN + R128_ALBUM_GAIN tags. Returns False if unwritable."""
    try:
        audio = mutagen.File(path)
    except Exception:
        return False
    if audio is None or audio.tags is None:
        return False
    try:
        audio.tags["R128_TRACK_GAIN"] = str(track_gain_q78)
        audio.tags["R128_ALBUM_GAIN"] = str(album_gain_q78)
        audio.save()
        return True
    except Exception:
        return False


def normalize_album(paths: list[Path]) -> int:
    """Measure + write per-track and album R128 gain across an album.

    Returns the number of tracks tagged. Best-effort: if any track can't be
    measured we still tag the rest with the album gain derived from whatever was
    measured; if nothing measured, nothing is written. Never raises.
    """
    if not paths:
        return 0
    measured: list[tuple[Path, float]] = []
    for p in paths:
        loud = measure_loudness(p)
        if loud is not None:
            measured.append((p, loud))
    if not measured:
        return 0
    album = album_loudness([loud for _, loud in measured])
    album_q78 = r128_q78(album)
    tagged = 0
    measured_by_path = dict(measured)
    for p in paths:
        loud = measured_by_path.get(p)
        if loud is None:
            continue
        if write_r128(p, r128_q78(loud), album_q78):
            tagged += 1
    return tagged