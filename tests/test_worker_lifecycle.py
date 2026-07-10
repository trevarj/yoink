"""Worker recovery and active-album cancellation regressions."""

from __future__ import annotations

import threading
from dataclasses import replace

from yoink.config import Config
from yoink.jobs import db as dbmod
from yoink.jobs.db import Database
from yoink.jobs.worker import Worker
from yoink.models import Release, Track

RELEASE = Release(
    mbid="lifecycle-release",
    title="Lifecycle Album",
    artist="Lifecycle Artist",
    artist_mbid="artist",
    date="2026",
    year=2026,
    country="XW",
    track_count=1,
    tracks=(Track(1, 1, "One", "Lifecycle Artist", 180_000, "recording"),),
)


def _setup(tmp_path):
    cfg = replace(
        Config(state_dir=tmp_path / "state", music_dir=tmp_path / "music"),
        download_concurrency=1,
        min_audio_bitrate=0.0,
        replaygain=False,
        tagger="mutagen",
    )
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    album_id = db.enqueue_release(RELEASE)
    assert album_id is not None
    return cfg, db, album_id


def test_album_exception_is_recorded_and_ui_retry_makes_it_claimable(tmp_path):
    cfg, db, album_id = _setup(tmp_path)
    worker = Worker(cfg, db, poll_interval=0.001)
    worker._mb.get_release = lambda _mbid: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("MusicBrainz unavailable")
    )
    original_claim = db.claim_next_album
    claims = 0

    def claim_once():
        nonlocal claims
        claims += 1
        if claims == 1:
            return original_claim()
        worker.stop()
        return None

    db.claim_next_album = claim_once  # type: ignore[method-assign]
    worker.run()

    failed = db.get_album_job(album_id)
    assert failed is not None
    assert failed.status == dbmod.ALBUM_FAILED
    assert failed.error == "MusicBrainz unavailable"
    assert db.list_tracks(album_id)[0].status == dbmod.TRACK_QUEUED

    assert db.requeue_album(album_id) == 1
    retried = db.get_album_job(album_id)
    assert retried is not None
    assert retried.status == dbmod.ALBUM_QUEUED
    assert retried.error is None
    assert original_claim() is not None


def test_recording_album_failure_resets_stranded_inflight_track(tmp_path):
    _cfg, db, album_id = _setup(tmp_path)
    track = db.list_tracks(album_id)[0]
    db.update_track(track.id, status=dbmod.TRACK_DOWNLOADING)

    db.fail_album(album_id, "tagger crashed")

    failed = db.get_album_job(album_id)
    assert failed is not None
    assert failed.status == dbmod.ALBUM_FAILED
    assert failed.error == "tagger crashed"
    assert db.get_track(track.id).status == dbmod.TRACK_QUEUED


def test_removing_active_album_blocks_final_library_write(tmp_path, monkeypatch):
    cfg, db, album_id = _setup(tmp_path)
    worker = Worker(cfg, db, poll_interval=0.001)
    worker._mb.get_release = lambda _mbid: RELEASE  # type: ignore[method-assign]
    worker._art.front_cover = lambda _mbid: None  # type: ignore[method-assign]
    worker._yt.find_album = lambda *_args: None  # type: ignore[method-assign]
    worker._resolve_video_id = (  # type: ignore[method-assign]
        lambda *_args: ("AAAAAAAAAAA", 100.0, "matched")
    )

    downloading = threading.Event()
    release_download = threading.Event()

    def download(_video_id, _progress_cb=None):
        downloading.set()
        assert release_download.wait(5)
        staged = cfg.staging_dir / "cancelled.opus"
        staged.write_bytes(b"audio")
        return staged

    worker._dl.download = download  # type: ignore[method-assign]
    placements: list[object] = []

    def place(*args, **kwargs):
        placements.append((args, kwargs))
        final = cfg.music_dir / "should-not-exist.opus"
        final.write_bytes(b"audio")
        return final

    monkeypatch.setattr("yoink.jobs.worker.mutagen_tagger.place", place)

    runner = threading.Thread(target=worker.run)
    runner.start()
    assert downloading.wait(5)
    assert worker.cancel_album(album_id)
    db.delete_album(album_id)
    release_download.set()
    worker.stop()
    runner.join(5)

    assert not runner.is_alive()
    assert db.get_album_job(album_id) is None
    assert placements == []
    assert not (cfg.music_dir / "should-not-exist.opus").exists()
