"""Focused tests for durable retries after transient download failures."""

from __future__ import annotations

from dataclasses import replace

from yoink.config import Config
from yoink.jobs import db as dbmod
from yoink.jobs.db import Database
from yoink.jobs.worker import Worker
from yoink.models import Release, Track
from yoink.youtube.downloader import DownloadError

RELEASE = Release(
    mbid="retry-release",
    title="Retry Album",
    artist="Retry Artist",
    artist_mbid="artist",
    date="2026",
    year=2026,
    country="XW",
    track_count=2,
    tracks=(
        Track(1, 1, "Transient", "Retry Artist", 180_000, "recording-1"),
        Track(2, 1, "Review", "Retry Artist", 190_000, "recording-2"),
    ),
)


def test_retryable_track_requeues_album_until_attempt_budget_exhausted(tmp_path):
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
    tracks = db.list_tracks(album_id)
    transient, review = tracks
    db.update_track(
        review.id,
        status=dbmod.TRACK_NEEDS_REVIEW,
        error="manual decision required",
    )

    worker = Worker(cfg, db)
    worker._mb.get_release = lambda _mbid: RELEASE  # type: ignore[method-assign]
    worker._art.front_cover = lambda _mbid: None  # type: ignore[method-assign]
    worker._yt.find_album = lambda *_args: None  # type: ignore[method-assign]
    resolved: list[str] = []

    def resolve(track, _album_match, _index):
        resolved.append(track.title)
        return "AAAAAAAAAAA", 100.0, "matched"

    worker._resolve_video_id = resolve  # type: ignore[method-assign]

    def fail_download(_video_id, _progress_cb=None):
        raise DownloadError("temporary network failure")

    worker._dl.download = fail_download  # type: ignore[method-assign]

    first_claim = db.claim_next_album()
    assert first_claim is not None
    worker._process_album(first_claim)

    after_first = db.get_track(transient.id)
    assert after_first is not None
    assert after_first.status == dbmod.TRACK_QUEUED
    assert after_first.attempts == 1
    assert db.get_album_job(album_id).status == dbmod.ALBUM_QUEUED

    second_claim = db.claim_next_album()
    assert second_claim is not None
    worker._process_album(second_claim)

    after_second = db.get_track(transient.id)
    assert after_second is not None
    assert after_second.status == dbmod.TRACK_FAILED
    assert after_second.attempts == 2
    assert db.get_album_job(album_id).status == dbmod.ALBUM_FAILED
    assert db.claim_next_album() is None
    assert resolved == ["Transient", "Transient"]


def test_recompute_makes_queued_track_album_claimable(tmp_path):
    db = Database(tmp_path / "queue.db")
    album_id = db.enqueue_release(RELEASE)
    assert album_id is not None
    tracks = db.list_tracks(album_id)
    db.update_track(tracks[0].id, status=dbmod.TRACK_QUEUED, attempts=1)
    db.update_track(tracks[1].id, status=dbmod.TRACK_DONE)
    db.set_album_status(album_id, dbmod.ALBUM_DOWNLOADING)

    assert db.recompute_album_status(album_id) == dbmod.ALBUM_QUEUED
    claimed = db.claim_next_album()
    assert claimed is not None and claimed.id == album_id
