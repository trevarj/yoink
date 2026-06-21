"""Tests for the beets duplicate-skip fallback in _finish_beets (no network).

When beets skips importing a staged track (e.g. the album is already in its
isolated lib from a prior run -- the requeue case), the worker must fall back to
direct mutagen tagging so the file still lands in the library instead of being
silently dropped while the DB marks it done.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from yoink.config import Config
from yoink.jobs import db as dbmod
from yoink.jobs.db import Database
from yoink.jobs.worker import Worker
from yoink.models import Release, Track
from yoink.tagging import mutagen_tagger

REL = Release(
    mbid="b3277fec", title="No Pressure (LP)", artist="No Pressure", artist_mbid="a",
    date="2024", year=2024, country="XW", track_count=2,
    tracks=(
        # Track fields are (position, disc, title, artist, duration_ms, recording_mbid).
        Track(1, 1, "Big Man", "No Pressure", 200000, "rec1"),
        Track(2, 1, "Sour", "No Pressure", 180000, "rec2"),
    ),
)


class _FakeBeets:
    """Stand-in for BeetsTagger. ``move_staged`` simulates beets moving the
    staged file out (import success) vs leaving it (duplicate-skip)."""

    def __init__(self, move_staged: bool) -> None:
        self.move_staged = move_staged
        self.imported: list[str] = []

    def import_album(self, album_dir: Path, mb_release_id: str) -> str:
        self.imported.append(str(album_dir))
        if self.move_staged:
            for p in Path(album_dir).glob("*"):
                if p.is_file():
                    p.unlink()
        return ""


def _setup(move_staged: bool):
    tmp = Path(tempfile.mkdtemp())
    cfg = replace(
        Config(state_dir=tmp / "state", music_dir=tmp / "music", tagger="beets"),
        min_audio_bitrate=0.0,
    )
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    album_id = db.enqueue_release(REL)
    album = db.get_album_job(album_id)
    album_dir = cfg.staging_dir / f"album_{album_id}"
    album_dir.mkdir(parents=True, exist_ok=True)

    worker = Worker(cfg, db)
    worker._beets = _FakeBeets(move_staged)  # type: ignore[assignment]
    worker._mb.get_release = lambda _mbid: REL  # type: ignore[assignment]

    calls: dict[str, list] = {"place": [], "cover": [], "normalize": []}
    orig_normalize = mutagen_tagger.normalize_featured_artists

    def fake_place(staged, music_dir, release, track, art=None):
        dest = music_dir / f"{track.position:02d} {track.title}.opus"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(dest))
        calls["place"].append(str(dest))
        return dest

    def fake_cover(dest, src):
        calls["cover"].append((str(dest), str(src)))
        return True

    def fake_normalize(_path):
        calls["normalize"].append(str(_path))

    mutagen_tagger.place = fake_place  # type: ignore[assignment]
    mutagen_tagger.embed_cover_from = fake_cover  # type: ignore[assignment]
    mutagen_tagger.normalize_featured_artists = fake_normalize  # type: ignore[assignment]
    return cfg, db, worker, album, album_dir, calls, orig_normalize


def _stage_track(db, album_id, disc, track_no, album_dir):
    row = next(t for t in db.list_tracks(album_id) if t.disc_no == disc and t.track_no == track_no)
    staged = album_dir / f"{disc}-{track_no:02d} {row.title}.opus"
    staged.write_bytes(b"audio")
    db.update_track(row.id, status=dbmod.TRACK_TAGGED, staging_path=str(staged))
    return row


def test_beets_skip_falls_back_to_mutagen(monkeypatch):
    cfg, db, worker, album, album_dir, calls, orig = _setup(move_staged=False)
    try:
        row = _stage_track(db, album.id, 1, 1, album_dir)  # track 1 only
        # A done sibling (track 2) with a real file so cover copy is exercised.
        sib = next(t for t in db.list_tracks(album.id) if t.track_no == 2)
        sib_final = cfg.music_dir / "done.opus"
        sib_final.write_bytes(b"audio")
        db.update_track(sib.id, status=dbmod.TRACK_DONE, final_path=str(sib_final))

        worker._finish_beets(album, album_dir)

        got = db.get_track(row.id)
        assert got.status == dbmod.TRACK_DONE
        assert got.final_path and Path(got.final_path).exists()
        assert calls["place"], "fallback should have placed the staged file"
        assert calls["cover"], "should copy cover from the done sibling"
        assert calls["normalize"], "should strip featured on the fallback file"
    finally:
        mutagen_tagger.normalize_featured_artists = orig  # type: ignore[assignment]


def test_beets_normal_import_no_fallback():
    cfg, db, worker, album, album_dir, calls, orig = _setup(move_staged=True)
    try:
        row = _stage_track(db, album.id, 1, 1, album_dir)  # beets "moves" it out
        worker._finish_beets(album, album_dir)
        got = db.get_track(row.id)
        assert got.status == dbmod.TRACK_DONE
        # beets handled it -> no mutagen fallback, no cover copy.
        assert calls["place"] == []
        assert calls["cover"] == []
    finally:
        mutagen_tagger.normalize_featured_artists = orig  # type: ignore[assignment]


def test_beets_skip_no_mb_track_fails_clean():
    cfg, db, worker, album, album_dir, calls, orig = _setup(move_staged=False)
    try:
        row = _stage_track(db, album.id, 1, 1, album_dir)
        row = db.get_track(row.id)
        # Staged file is present (beets skipped it) but we have no MB track to
        # tag it with -> the fallback can't recover and must fail the track.
        worker._beets_skip_fallback(row, None, REL, album.id)
        got = db.get_track(row.id)
        assert got.status == dbmod.TRACK_FAILED
        assert calls["place"] == []
    finally:
        mutagen_tagger.normalize_featured_artists = orig  # type: ignore[assignment]