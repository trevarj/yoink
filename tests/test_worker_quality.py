"""Unit tests for the worker's audio-quality gate (no network).

Exercises ``_process_track`` directly with a fake downloader + tagger so the
quality-gate branching can be asserted without ffmpeg or YouTube.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from yoink.config import Config
from yoink.jobs import db as dbmod
from yoink.jobs.db import Database
from yoink.jobs.worker import Worker
from yoink.models import Release, Track
from yoink.youtube.downloader import AudioQuality

REL = Release(
    mbid="rel1", title="Discovery", artist="Daft Punk", artist_mbid="a",
    date="2001", year=2001, country="XW", track_count=1,
    tracks=(Track(1, 1, "One More Time", "Daft Punk", 320000, "rec1"),),
)
VID = "AAAAAAAAAAA"


def _setup(min_br: float, manual: str | None = None):
    tmp = Path(tempfile.mkdtemp())
    cfg = replace(
        Config(state_dir=tmp / "state", music_dir=tmp / "music", tagger="mutagen"),
        min_audio_bitrate=min_br,
    )
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    album_id = db.enqueue_release(REL)
    row = db.list_tracks(album_id)[0]
    if manual:
        db.set_manual_source(row.id, manual)
        row = db.get_track(row.id)  # type: ignore[assignment]

    worker = Worker(cfg, db)

    calls: dict[str, list] = {"probe": [], "download": []}

    def fake_resolve(track, album_match, index):
        return VID, 50.0, "matched"

    worker._resolve_video_id = fake_resolve  # type: ignore[method-assign]

    def fake_probe(video_id):
        calls["probe"].append(video_id)
        return worker._probe_result  # set per-test

    worker._dl.probe_audio = fake_probe  # type: ignore[method-assign]

    def fake_download(video_id, progress_cb=None):
        calls["download"].append(video_id)
        dest = cfg.staging_dir / f"{video_id}.opus"
        dest.write_bytes(b"audio")
        return dest

    worker._dl.download = fake_download  # type: ignore[method-assign]

    # Avoid real audio tagging: just return a final path.
    import yoink.jobs.worker as wmod

    def fake_place(staged, music_dir, release, track, art=None):
        dest = music_dir / "out.opus"
        return dest

    wmod.mutagen_tagger.place = fake_place  # type: ignore[assignment]

    album_dir = cfg.staging_dir / f"album_{album_id}"
    return cfg, db, worker, row, album_dir, calls


def _run(worker, db, row, album_dir):
    worker._process_track(REL, REL.tracks[0], row, None, 0, album_dir)
    return db.get_track(row.id)


def test_above_threshold_downloads_and_records_bitrate():
    cfg, db, worker, row, album_dir, calls = _setup(min_br=128.0)
    worker._probe_result = AudioQuality(VID, 256.0, "webm", "opus", None)
    t = _run(worker, db, row, album_dir)
    assert t.status == dbmod.TRACK_DONE
    assert t.audio_bitrate == 256.0
    assert calls["download"] == [VID]


def test_below_threshold_flags_needs_review():
    cfg, db, worker, row, album_dir, calls = _setup(min_br=128.0)
    worker._probe_result = AudioQuality(VID, 64.0, "webm", "opus", None)
    t = _run(worker, db, row, album_dir)
    assert t.status == dbmod.TRACK_NEEDS_REVIEW
    assert t.audio_bitrate == 64.0
    assert "low audio bitrate" in (t.error or "")
    assert calls["download"] == []  # never downloaded


def test_unknown_bitrate_proceeds():
    cfg, db, worker, row, album_dir, calls = _setup(min_br=128.0)
    worker._probe_result = None  # unmeasurable
    t = _run(worker, db, row, album_dir)
    assert t.status == dbmod.TRACK_DONE
    assert t.audio_bitrate is None
    assert calls["download"] == [VID]


def test_disabled_probe_never_called():
    cfg, db, worker, row, album_dir, calls = _setup(min_br=0.0)
    worker._probe_result = AudioQuality(VID, 64.0, "webm", "opus", None)
    t = _run(worker, db, row, album_dir)
    assert t.status == dbmod.TRACK_DONE  # would have been rejected if probed
    assert calls["probe"] == []


def test_manual_pick_bypasses_gate():
    cfg, db, worker, row, album_dir, calls = _setup(min_br=128.0, manual=VID)
    # Even a low-quality probe result must not block a manual pick.
    worker._probe_result = AudioQuality(VID, 64.0, "webm", "opus", None)
    t = _run(worker, db, row, album_dir)
    assert t.status == dbmod.TRACK_DONE
    assert calls["probe"] == []  # gate skipped for manual picks
    assert calls["download"] == [VID]