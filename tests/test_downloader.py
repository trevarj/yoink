"""Pure unit tests for the audio-quality probe (no network).

`probe_audio` must never crash: it returns None on failure and falls back from
the often-None `abr` to `tbr` (the trustworthy bitrate field for YouTube opus).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yoink.config import Config
from yoink.youtube import downloader as dlmod
from yoink.youtube.downloader import AudioQuality, Downloader

VID = "AAAAAAAAAAA"


def _downloader(tmp_path: Path) -> Downloader:
    cfg = Config(state_dir=tmp_path)
    cfg.staging_dir.mkdir(parents=True, exist_ok=True)
    return Downloader(cfg)


class _FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL that yields a fixed info dict."""

    info: dict | Exception = {}

    def __init__(self, opts: dict) -> None:
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict:
        if isinstance(self.info, Exception):
            raise self.info
        return self.info


@pytest.fixture(autouse=True)
def _patch_ydl(monkeypatch):
    """Save/restore the per-test info on the fake YoutubeDL."""
    _FakeYDL.info = {}
    monkeypatch.setattr(dlmod.yt_dlp, "YoutubeDL", _FakeYDL)
    yield
    _FakeYDL.info = {}


def test_probe_picks_highest_tbr_audio_only(tmp_path):
    _FakeYDL.info = {
        "formats": [
            {"vcodec": "none", "acodec": "opus", "ext": "webm", "tbr": 140},
            {"vcodec": "none", "acodec": "opus", "ext": "webm", "tbr": 256},
            {"vcodec": "av01.0", "acodec": "none", "ext": "mp4", "tbr": 1500},
        ]
    }
    q = _downloader(tmp_path).probe_audio(VID)
    assert q == AudioQuality(
        video_id=VID, bitrate_kbps=256.0, ext="webm", acodec="opus", filesize=None
    )


def test_probe_falls_back_to_abr_when_tbr_missing(tmp_path):
    # YouTube often leaves tbr=None and only sets abr.
    _FakeYDL.info = {
        "formats": [
            {"vcodec": "none", "acodec": "mp4a", "ext": "m4a", "abr": 128, "tbr": None},
        ]
    }
    q = _downloader(tmp_path).probe_audio(VID)
    assert q is not None
    assert q.bitrate_kbps == 128.0


def test_probe_ignores_video_formats(tmp_path):
    _FakeYDL.info = {
        "formats": [
            {"vcodec": "av01.0", "acodec": "none", "ext": "mp4", "tbr": 9999},
        ]
    }
    # Only video formats present: the info dict itself isn't audio-only, so the
    # single-format fallback path is taken; its bitrate fields are absent -> None.
    q = _downloader(tmp_path).probe_audio(VID)
    assert q is not None
    assert q.bitrate_kbps is None


def test_probe_single_audio_info_dict(tmp_path):
    # No "formats" list -- info is itself the audio format.
    _FakeYDL.info = {"vcodec": "none", "acodec": "opus", "ext": "webm", "tbr": 160}
    q = _downloader(tmp_path).probe_audio(VID)
    assert q is not None
    assert q.bitrate_kbps == 160.0


def test_probe_zero_bitrate_is_unknown(tmp_path):
    _FakeYDL.info = {"formats": [{"vcodec": "none", "acodec": "opus"}]}
    q = _downloader(tmp_path).probe_audio(VID)
    assert q is not None
    assert q.bitrate_kbps is None  # 0 -> None so callers proceed, not reject


def test_probe_returns_none_on_download_error(tmp_path):
    _FakeYDL.info = dlmod.yt_dlp.utils.DownloadError("geo")
    assert _downloader(tmp_path).probe_audio(VID) is None


def test_probe_returns_none_on_falsy_info(tmp_path):
    # An empty info dict (falsy) -> None.
    _FakeYDL.info = {}
    assert _downloader(tmp_path).probe_audio(VID) is None


def test_probe_empty_formats_falls_back_to_info(tmp_path):
    # "formats" present but empty -> info dict treated as the single format.
    _FakeYDL.info = {"formats": []}
    q = _downloader(tmp_path).probe_audio(VID)
    assert q is not None
    assert q.bitrate_kbps is None