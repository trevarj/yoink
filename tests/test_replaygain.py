"""Tests for ReplayGain (R128) album normalization.

The math is exercised unconditionally; the ffmpeg + mutagen path is guarded on
ffmpeg being present (the rest of the suite avoids ffmpeg).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.oggopus import OggOpus

from yoink.tagging import replaygain

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_r128_q78_reference_is_zero():
    # -23 LUFS is the R128 reference -> 0 dB gain -> Q7.8 value 0.
    assert replaygain.r128_q78(-23.0) == 0


def test_r128_q78_louder_than_reference_is_attenuation():
    # -18 LUFS is 5 dB above the -23 reference -> apply -5 dB -> -1280.
    # Matches beets/mediafile's Q7.8 storage: round(256 * (ref - loudness)).
    assert replaygain.r128_q78(-18.0) == -1280


def test_r128_q78_quieter_than_reference_is_boost():
    # -28 LUFS is 5 dB below reference -> apply +5 dB -> +1280.
    assert replaygain.r128_q78(-28.0) == 1280


def test_r128_q78_clamps_to_int16_range():
    # Gain beyond +/-128 dB (256 * 128 = 32768) saturates the opus Q7.8 range.
    assert replaygain.r128_q78(300.0) == -(1 << 15)  # absurdly loud -> max cut
    assert replaygain.r128_q78(-300.0) == (1 << 15) - 1  # absurdly quiet -> max boost


def test_album_loudness_is_mean():
    assert replaygain.album_loudness([-20.0, -24.0]) == pytest.approx(-22.0)


def test_album_loudness_empty_falls_back_to_reference():
    assert replaygain.album_loudness([]) == -23.0


def _make_opus(path: Path, volume_db: float) -> None:
    """Generate a 2s sine tone opus at a given volume (ffmpeg)."""
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-af",
            f"volume={volume_db}dB",
            "-c:a",
            "libopus",
            "-b:a",
            "96k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not available")
def test_measure_loudness_reads_a_value(tmp_path: Path):
    p = tmp_path / "t.opus"
    _make_opus(p, volume_db=0.0)
    loud = replaygain.measure_loudness(p)
    assert loud is not None
    assert -60.0 < loud < 0.0


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not available")
def test_normalize_album_writes_r128_tags_with_shared_album_gain(tmp_path: Path):
    quiet = tmp_path / "q.opus"
    loud = tmp_path / "l.opus"
    _make_opus(quiet, volume_db=-12.0)
    _make_opus(loud, volume_db=0.0)

    tagged = replaygain.normalize_album([quiet, loud])
    assert tagged == 2

    q = OggOpus(quiet)
    ell = OggOpus(loud)
    # Both tracks share the album gain; track gains differ with their loudness.
    assert q["R128_ALBUM_GAIN"] == ell["R128_ALBUM_GAIN"]
    assert q["R128_TRACK_GAIN"] != ell["R128_TRACK_GAIN"]
    # Reference-relative: the quieter track needs a positive (boost) gain.
    assert int(q["R128_TRACK_GAIN"][0]) > int(ell["R128_TRACK_GAIN"][0])


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not available")
def test_normalize_album_empty_is_noop(tmp_path: Path):
    assert replaygain.normalize_album([]) == 0


def test_measure_loudness_missing_file_returns_none(tmp_path: Path):
    assert replaygain.measure_loudness(tmp_path / "nope.opus") is None