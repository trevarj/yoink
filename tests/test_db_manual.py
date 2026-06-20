"""Pure unit tests for the manual-source DB flow (no network)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from yoink.jobs import db as dbmod
from yoink.jobs.db import Database
from yoink.models import Release, Track

REL = Release(
    mbid="rel1", title="Discovery", artist="Daft Punk", artist_mbid="a",
    date="2001", year=2001, country="XW", track_count=2,
    tracks=(
        Track(1, 1, "One More Time", "Daft Punk", 320000, "rec1"),
        Track(2, 1, "Aerodynamic", "Daft Punk", 213000, "rec2"),
    ),
)


def _db() -> Database:
    return Database(Path(tempfile.mkdtemp()) / "q.db")


def test_set_manual_source_requeues_track_and_album():
    db = _db()
    album_id = db.enqueue_release(REL)
    tracks = db.list_tracks(album_id)
    t = tracks[0]
    # Simulate the worker having flagged it.
    db.update_track(t.id, status=dbmod.TRACK_NEEDS_REVIEW, error="below threshold")
    db.set_album_status(album_id, dbmod.ALBUM_FAILED)

    db.set_manual_source(t.id, "ABCDEFGHIJK")

    got = db.get_track(t.id)
    assert got.manual_video_id == "ABCDEFGHIJK"
    assert got.yt_video_id == "ABCDEFGHIJK"
    assert got.status == dbmod.TRACK_QUEUED
    assert got.error is None and got.attempts == 0
    assert db.get_album_job(album_id).status == dbmod.ALBUM_QUEUED


def test_migration_adds_column_on_existing_db(tmp_path):
    # Build a DB without the manual_video_id column, then reopen.
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE album_jobs (id INTEGER PRIMARY KEY, mb_release_id TEXT, "
        "artist TEXT, album TEXT, year INTEGER, total_tracks INTEGER, status TEXT, "
        "created_at TEXT, updated_at TEXT);"
        "CREATE TABLE track_jobs (id INTEGER PRIMARY KEY, album_job_id INTEGER, "
        "disc_no INTEGER, track_no INTEGER, title TEXT, artist TEXT, duration_ms INTEGER, "
        "yt_video_id TEXT, match_score REAL, status TEXT NOT NULL, staging_path TEXT, "
        "final_path TEXT, attempts INTEGER DEFAULT 0, error TEXT, updated_at TEXT);"
    )
    con.close()

    Database(path)  # triggers migration
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(track_jobs)")}
    con.close()
    assert "manual_video_id" in cols
    assert "audio_bitrate" in cols


def test_audio_bitrate_round_trips():
    db = _db()
    album_id = db.enqueue_release(REL)
    t = db.list_tracks(album_id)[0]
    assert db.get_track(t.id).audio_bitrate is None  # default
    db.update_track(t.id, audio_bitrate=256.0)
    got = db.get_track(t.id)
    assert got.audio_bitrate == 256.0
