"""SQLite-backed job queue.

Decouples browsing (which enqueues albums) from downloading (a background
worker), and survives restarts: on startup any in-flight rows are reset to a
retryable state so work resumes. The DB is the source of truth for status and
``needs_review``; yt-dlp's own download-archive is a second dedupe layer.

Connections are short-lived and per-call so the UI thread and worker thread
never share a handle; WAL mode lets a reader and the single writer coexist.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..models import Release

# --- status vocabularies ---------------------------------------------------
ALBUM_QUEUED = "queued"
ALBUM_RESOLVING = "resolving"
ALBUM_DOWNLOADING = "downloading"
ALBUM_DONE = "done"
ALBUM_FAILED = "failed"

TRACK_QUEUED = "queued"
TRACK_MATCHING = "matching"
TRACK_NEEDS_REVIEW = "needs_review"
TRACK_DOWNLOADING = "downloading"
TRACK_TAGGED = "tagged"
TRACK_DONE = "done"
TRACK_FAILED = "failed"

# States that mean "was in flight" -> reset to queued on startup.
_ALBUM_INFLIGHT = (ALBUM_RESOLVING, ALBUM_DOWNLOADING)
_TRACK_INFLIGHT = (TRACK_MATCHING, TRACK_DOWNLOADING)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS album_jobs (
  id INTEGER PRIMARY KEY,
  mb_release_id TEXT,
  artist TEXT, album TEXT, year INTEGER, total_tracks INTEGER,
  status TEXT NOT NULL,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS track_jobs (
  id INTEGER PRIMARY KEY,
  album_job_id INTEGER NOT NULL REFERENCES album_jobs(id) ON DELETE CASCADE,
  disc_no INTEGER, track_no INTEGER, title TEXT, artist TEXT,
  duration_ms INTEGER,
  yt_video_id TEXT, match_score REAL,
  status TEXT NOT NULL,
  staging_path TEXT, final_path TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  manual_video_id TEXT,
  audio_bitrate REAL,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_track_status ON track_jobs(status);
CREATE INDEX IF NOT EXISTS idx_track_album ON track_jobs(album_job_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_album_release ON album_jobs(mb_release_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class AlbumJob:
    id: int
    mb_release_id: str | None
    artist: str
    album: str
    year: int | None
    total_tracks: int
    status: str

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> AlbumJob:
        return cls(
            id=r["id"],
            mb_release_id=r["mb_release_id"],
            artist=r["artist"],
            album=r["album"],
            year=r["year"],
            total_tracks=r["total_tracks"],
            status=r["status"],
        )


@dataclass
class TrackJob:
    id: int
    album_job_id: int
    disc_no: int
    track_no: int
    title: str
    artist: str
    duration_ms: int | None
    yt_video_id: str | None
    match_score: float | None
    status: str
    staging_path: str | None
    final_path: str | None
    attempts: int
    error: str | None
    manual_video_id: str | None = None
    audio_bitrate: float | None = None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> TrackJob:
        return cls(
            id=r["id"],
            album_job_id=r["album_job_id"],
            disc_no=r["disc_no"],
            track_no=r["track_no"],
            title=r["title"],
            artist=r["artist"],
            duration_ms=r["duration_ms"],
            yt_video_id=r["yt_video_id"],
            match_score=r["match_score"],
            status=r["status"],
            staging_path=r["staging_path"],
            final_path=r["final_path"],
            attempts=r["attempts"],
            error=r["error"],
            manual_video_id=r["manual_video_id"],
            audio_bitrate=r["audio_bitrate"],
        )


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # Migrate older DBs created before manual_video_id existed.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(track_jobs)")}
            if "manual_video_id" not in cols:
                c.execute("ALTER TABLE track_jobs ADD COLUMN manual_video_id TEXT")
            if "audio_bitrate" not in cols:
                c.execute("ALTER TABLE track_jobs ADD COLUMN audio_bitrate REAL")

    # --- enqueue -----------------------------------------------------------
    def enqueue_release(self, release: Release) -> int | None:
        """Insert an album + its tracks. Returns the album_job id, or None if
        this release is already queued (unique mb_release_id)."""
        now = _now()
        with self._conn() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO album_jobs
                   (mb_release_id, artist, album, year, total_tracks, status,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    release.mbid,
                    release.artist,
                    release.title,
                    release.year,
                    release.track_count,
                    ALBUM_QUEUED,
                    now,
                    now,
                ),
            )
            if cur.rowcount == 0:
                return None  # already present
            album_id = cur.lastrowid
            c.executemany(
                """INSERT INTO track_jobs
                   (album_job_id, disc_no, track_no, title, artist, duration_ms,
                    status, attempts, updated_at)
                   VALUES (?,?,?,?,?,?,?,0,?)""",
                [
                    (
                        album_id,
                        t.disc,
                        t.position,
                        t.title,
                        t.artist,
                        t.duration_ms,
                        TRACK_QUEUED,
                        now,
                    )
                    for t in release.tracks
                ],
            )
            return album_id

    # --- queries -----------------------------------------------------------
    def list_album_jobs(self) -> list[AlbumJob]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM album_jobs ORDER BY created_at DESC"
            ).fetchall()
        return [AlbumJob.from_row(r) for r in rows]

    def get_album_job(self, album_id: int) -> AlbumJob | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM album_jobs WHERE id=?", (album_id,)).fetchone()
        return AlbumJob.from_row(r) if r else None

    def list_tracks(self, album_id: int) -> list[TrackJob]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM track_jobs WHERE album_job_id=? ORDER BY disc_no, track_no",
                (album_id,),
            ).fetchall()
        return [TrackJob.from_row(r) for r in rows]

    def get_track(self, track_id: int) -> TrackJob | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM track_jobs WHERE id=?", (track_id,)).fetchone()
        return TrackJob.from_row(r) if r else None

    def set_manual_source(self, track_id: int, video_id: str) -> None:
        """Force a specific YouTube videoId for one track and requeue it.

        The worker downloads this id verbatim (no matching). Re-queues the
        parent album so the worker picks the track up again.
        """
        now = _now()
        with self._conn() as c:
            row = c.execute(
                "SELECT album_job_id FROM track_jobs WHERE id=?", (track_id,)
            ).fetchone()
            if row is None:
                return
            c.execute(
                "UPDATE track_jobs SET manual_video_id=?, yt_video_id=?, status=?, "
                "attempts=0, error=NULL, match_score=NULL, updated_at=? WHERE id=?",
                (video_id, video_id, TRACK_QUEUED, now, track_id),
            )
            c.execute(
                "UPDATE album_jobs SET status=?, updated_at=? WHERE id=?",
                (ALBUM_QUEUED, now, row["album_job_id"]),
            )

    def album_progress(self, album_id: int) -> dict[str, int]:
        """Count of tracks per status for an album (for the progress UI)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) n FROM track_jobs WHERE album_job_id=? GROUP BY status",
                (album_id,),
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def next_queued_album(self) -> AlbumJob | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM album_jobs WHERE status=? ORDER BY created_at LIMIT 1",
                (ALBUM_QUEUED,),
            ).fetchone()
        return AlbumJob.from_row(r) if r else None

    def claim_next_album(self) -> AlbumJob | None:
        """Atomically grab the oldest queued album and mark it resolving.

        BEGIN IMMEDIATE takes the write lock up front so two workers can't claim
        the same row.
        """
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute(
                    "SELECT * FROM album_jobs WHERE status=? ORDER BY created_at LIMIT 1",
                    (ALBUM_QUEUED,),
                ).fetchone()
                if r is None:
                    c.execute("COMMIT")
                    return None
                c.execute(
                    "UPDATE album_jobs SET status=?, updated_at=? WHERE id=?",
                    (ALBUM_RESOLVING, _now(), r["id"]),
                )
                c.execute("COMMIT")
                return AlbumJob.from_row(r)
            except Exception:
                c.execute("ROLLBACK")
                raise

    def pending_tracks(self, album_id: int) -> list[TrackJob]:
        """Tracks still needing work (queued)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM track_jobs WHERE album_job_id=? AND status=? "
                "ORDER BY disc_no, track_no",
                (album_id, TRACK_QUEUED),
            ).fetchall()
        return [TrackJob.from_row(r) for r in rows]

    # --- mutations ---------------------------------------------------------
    def set_album_status(self, album_id: int, status: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE album_jobs SET status=?, updated_at=? WHERE id=?",
                (status, _now(), album_id),
            )

    def update_track(self, track_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(
                f"UPDATE track_jobs SET {cols} WHERE id=?",
                (*fields.values(), track_id),
            )

    def bump_attempt(self, track_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE track_jobs SET attempts=attempts+1, updated_at=? WHERE id=?",
                (_now(), track_id),
            )

    def recompute_album_status(self, album_id: int) -> str:
        """Set the album's status from its tracks' aggregate and return it."""
        prog = self.album_progress(album_id)
        total = sum(prog.values())
        done = prog.get(TRACK_DONE, 0)
        terminal = done + prog.get(TRACK_FAILED, 0) + prog.get(TRACK_NEEDS_REVIEW, 0)
        if total and terminal == total:
            status = ALBUM_DONE if done == total else ALBUM_FAILED
        else:
            status = ALBUM_DOWNLOADING
        self.set_album_status(album_id, status)
        return status

    def delete_album(self, album_id: int) -> None:
        with self._conn() as c:
            # Explicit child delete in case foreign_keys pragma is unavailable.
            c.execute("DELETE FROM track_jobs WHERE album_job_id=?", (album_id,))
            c.execute("DELETE FROM album_jobs WHERE id=?", (album_id,))

    def requeue_album(self, album_id: int) -> int:
        """Reset failed/needs_review tracks to queued so the worker retries.

        Returns the number of tracks requeued.
        """
        now = _now()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE track_jobs SET status=?, attempts=0, error=NULL, updated_at=? "
                "WHERE album_job_id=? AND status IN (?,?)",
                (TRACK_QUEUED, now, album_id, TRACK_NEEDS_REVIEW, TRACK_FAILED),
            )
            n = cur.rowcount
            if n:
                c.execute(
                    "UPDATE album_jobs SET status=?, updated_at=? WHERE id=?",
                    (ALBUM_QUEUED, now, album_id),
                )
        return n

    # --- startup resume ----------------------------------------------------
    def reset_inflight(self) -> None:
        """Reset rows left mid-flight by a previous run so work resumes."""
        now = _now()
        with self._conn() as c:
            c.execute(
                f"UPDATE track_jobs SET status=?, updated_at=? WHERE status IN "
                f"({','.join('?' * len(_TRACK_INFLIGHT))})",
                (TRACK_QUEUED, now, *_TRACK_INFLIGHT),
            )
            c.execute(
                f"UPDATE album_jobs SET status=?, updated_at=? WHERE status IN "
                f"({','.join('?' * len(_ALBUM_INFLIGHT))})",
                (ALBUM_QUEUED, now, *_ALBUM_INFLIGHT),
            )
