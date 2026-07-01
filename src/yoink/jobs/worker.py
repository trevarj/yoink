"""Background download worker: matches, downloads, and tags queued albums.

One worker thread (or a few; albums are claimed atomically) pulls queued albums
and processes their tracks. The full MusicBrainz release is re-read from the
on-disk cache for authoritative tagging metadata; the matcher resolves each
track to a YouTube videoId (album-as-playlist when confident, else per-track
search). Anything below the match threshold is flagged ``needs_review`` rather
than guessed.

All durable state goes through the DB so the TUI can poll it; a transient
``progress_cb`` reports the live download fraction of the current track.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rapidfuzz import fuzz

from ..config import Config
from ..metadata.coverart import CoverArtArchive
from ..metadata.musicbrainz import MusicBrainz
from ..models import Release, Track
from ..tagging import mutagen_tagger, replaygain
from ..tagging.beets_tagger import BeetsError, BeetsTagger
from ..youtube.downloader import Downloader, DownloadError
from ..youtube.matcher import best_match
from ..youtube.search import YouTubeMusic
from . import db as dbmod
from .db import Database

# progress_cb(track_id, fraction|None, status)
ProgressCb = Callable[[int, float | None, str], None]

_MAX_ATTEMPTS = 2
_ALBUM_TITLE_MIN = 70.0  # fuzz ratio to trust an album-aligned videoId


class Worker(threading.Thread):
    def __init__(
        self,
        config: Config,
        db: Database,
        progress_cb: ProgressCb | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        super().__init__(daemon=True, name="yoink-worker")
        self.config = config
        self.db = db
        self.progress_cb = progress_cb
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._yt_lock = threading.Lock()  # serialize shared ytmusicapi searches
        self._mb = MusicBrainz(config)
        self._art = CoverArtArchive(config)
        self._yt = YouTubeMusic()
        self._dl = Downloader(config)
        self._beets = BeetsTagger(config) if config.tagger == "beets" else None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            album = self.db.claim_next_album()
            if album is None:
                self._stop.wait(self.poll_interval)
                continue
            try:
                self._process_album(album)
            except Exception as e:  # never let one album kill the worker
                self.db.set_album_status(album.id, dbmod.ALBUM_FAILED)
                self._emit(0, None, f"album {album.id} error: {e}")

    # --- album ------------------------------------------------------------
    def _process_album(self, album: dbmod.AlbumJob) -> None:
        if not album.mb_release_id:
            self.db.set_album_status(album.id, dbmod.ALBUM_FAILED)
            return
        release = self._mb.get_release(album.mb_release_id)
        album_art = self._art.front_cover(release.mbid)
        self.db.set_album_status(album.id, dbmod.ALBUM_DOWNLOADING)

        # Try the album-as-playlist path for clean, aligned videoIds.
        album_match = None
        try:
            album_match = self._yt.find_album(
                release.artist, release.title, release.track_count
            )
        except Exception:
            album_match = None

        rows = {(t.disc_no, t.track_no): t for t in self.db.list_tracks(album.id)}
        album_dir = self.config.staging_dir / f"album_{album.id}"
        if self._beets:
            album_dir.mkdir(parents=True, exist_ok=True)

        # Process the album's pending tracks concurrently (bounded) so a slow or
        # stalled track doesn't leave the rest sitting in 'queued'.
        pending = [
            (i, track, rows.get((track.disc, track.position)))
            for i, track in enumerate(release.tracks)
        ]
        pending = [
            (i, t, r)
            for (i, t, r) in pending
            if r is not None and r.status != dbmod.TRACK_DONE
        ]
        workers = max(1, self.config.download_concurrency)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(
                    self._process_track,
                    release,
                    t,
                    r,
                    album_match,
                    i,
                    album_dir,
                    album_art,
                )
                for (i, t, r) in pending
            ]
            for f in as_completed(futures):
                try:
                    f.result()  # exceptions are handled inside _process_track
                except Exception as e:  # defensive: never propagate
                    self._emit(0, None, f"track error: {e}")

        if self._beets:
            self._finish_beets(album, album_dir)
        elif self.config.replaygain:
            # Mutagen path has no beets import to compute gain, so do it here:
            # album-level R128 gain needs every track measured together.
            self._normalize_album(album.id)

        self.db.recompute_album_status(album.id)
        if not self._beets and album_dir.exists():
            shutil.rmtree(album_dir, ignore_errors=True)

    # --- track ------------------------------------------------------------
    def _resolve_video_id(
        self, track: Track, album_match, index: int
    ) -> tuple[str | None, float, str]:
        """Return (video_id, score, reason). None video_id -> needs review."""
        # Album path: trust the aligned videoId if its title roughly matches.
        if album_match and index < len(album_match.track_video_ids):
            vid = album_match.track_video_ids[index]
            yt_title = album_match.track_titles[index] if index < len(
                album_match.track_titles
            ) else ""
            if vid and fuzz.token_set_ratio(track.title, yt_title) >= _ALBUM_TITLE_MIN:
                return vid, 100.0, "album-aligned"

        # Per-track search + scoring. Serialize the shared ytmusicapi client.
        with self._yt_lock:
            candidates = self._yt.search_track(track.artist, track.title)
        result = best_match(
            track,
            candidates,
            gate_s=self.config.duration_gate_s,
            soft_s=self.config.duration_soft_s,
            min_score=self.config.min_match_score,
        )
        if result.accepted and result.candidate:
            return result.candidate.video_id, result.score, result.reason
        return None, result.score, result.reason

    def _process_track(
        self,
        release: Release,
        track: Track,
        row: dbmod.TrackJob,
        album_match,
        index: int,
        album_dir: Path,
        album_art=None,
    ) -> None:
        if self._stop.is_set():
            return
        self.db.update_track(row.id, status=dbmod.TRACK_MATCHING)
        if row.manual_video_id:
            # User picked this source explicitly -> skip matching, trust it.
            vid, score, reason = row.manual_video_id, -1.0, "manual"
        else:
            vid, score, reason = self._resolve_video_id(track, album_match, index)
        if vid is None:
            self.db.update_track(
                row.id, status=dbmod.TRACK_NEEDS_REVIEW, match_score=score, error=reason
            )
            self._emit(row.id, None, f"needs review: {reason}")
            return

        # Quality gate: reject low-bitrate sources into review rather than saving
        # a bad file. Manual picks bypass the gate (explicit user choice). A
        # probe that returns None (unmeasurable) proceeds without rejection.
        if self.config.min_audio_bitrate > 0 and not row.manual_video_id:
            q = self._dl.probe_audio(vid)
            br = q.bitrate_kbps if q else None
            if br is not None and br < self.config.min_audio_bitrate:
                self.db.update_track(
                    row.id,
                    status=dbmod.TRACK_NEEDS_REVIEW,
                    match_score=score,
                    audio_bitrate=br,
                    error=(
                        f"low audio bitrate ({br:.0f}k < "
                        f"{self.config.min_audio_bitrate:.0f}k)"
                    ),
                )
                self._emit(row.id, None, f"needs review: low bitrate {br:.0f}k")
                return
            if br is not None:
                self.db.update_track(row.id, audio_bitrate=br)

        self.db.update_track(
            row.id,
            status=dbmod.TRACK_DOWNLOADING,
            yt_video_id=vid,
            match_score=score,
            error=None,
        )
        self.db.bump_attempt(row.id)
        try:
            staged = self._dl.download(
                vid, lambda frac, st: self._emit(row.id, frac, st)
            )
        except DownloadError as e:
            status = (
                dbmod.TRACK_FAILED
                if row.attempts + 1 >= _MAX_ATTEMPTS
                else dbmod.TRACK_QUEUED
            )
            self.db.update_track(row.id, status=status, error=str(e)[:500])
            return

        # Pre-tag with authoritative metadata and replace YouTube thumbnails
        # with MusicBrainz-linked cover art when the archive has it.
        try:
            if self._beets:
                mutagen_tagger.write_tags(staged, release, track, album_art)
                dest = album_dir / _staged_name(track, staged.suffix)
                shutil.move(str(staged), str(dest))
                self.db.update_track(
                    row.id, status=dbmod.TRACK_TAGGED, staging_path=str(dest)
                )
            else:
                final = mutagen_tagger.place(
                    staged, self.config.music_dir, release, track, album_art
                )
                self._maybe_strip_featured(final)
                self.db.update_track(
                    row.id, status=dbmod.TRACK_DONE, final_path=str(final), error=None
                )
                self._emit(row.id, 1.0, "done")
        except Exception as e:
            self.db.update_track(row.id, status=dbmod.TRACK_FAILED, error=str(e)[:500])

    # --- beets finalize ----------------------------------------------------
    def _finish_beets(self, album: dbmod.AlbumJob, album_dir: Path) -> None:
        tracks = self.db.list_tracks(album.id)
        tagged = [t for t in tracks if t.status == dbmod.TRACK_TAGGED]
        if not tagged:
            return
        assert self._beets is not None
        try:
            self._beets.import_album(album_dir, album.mb_release_id or "")
        except BeetsError as e:
            for t in tagged:
                self.db.update_track(t.id, status=dbmod.TRACK_FAILED, error=str(e)[:500])
            return
        # beets moves imported files out of album_dir. A staged file still present
        # here was skipped -- typically a duplicate of an album beets already
        # imported on a requeue (e.g. after manual resolve). Fall back to direct
        # mutagen tagging so the track still lands in the library instead of being
        # silently dropped while the DB claims it's done.
        release = self._mb.get_release(album.mb_release_id)  # cached
        by_pos = {(tk.disc, tk.position): tk for tk in release.tracks}
        for t in tagged:
            tk = by_pos.get((t.disc_no, t.track_no))
            staged = Path(t.staging_path) if t.staging_path else None
            if staged and staged.exists():
                self._beets_skip_fallback(t, tk, release, album.id)
                continue
            final = (
                mutagen_tagger.final_path(self.config.music_dir, release, tk, ".opus")
                if tk
                else None
            )
            if final:
                self._maybe_strip_featured(final)
            self.db.update_track(
                t.id,
                status=dbmod.TRACK_DONE,
                final_path=str(final) if final else None,
                error=None,
            )
            self._emit(t.id, 1.0, "done")
        shutil.rmtree(album_dir, ignore_errors=True)

    def _beets_skip_fallback(
        self, t: dbmod.TrackJob, tk: Track | None, release: Release, album_id: int
    ) -> None:
        """Tag + place a track beets skipped importing, using its staged file."""
        staged = Path(t.staging_path) if t.staging_path else None
        if not staged or not staged.exists():
            self.db.update_track(
                t.id,
                status=dbmod.TRACK_FAILED,
                error="beets skipped import; no staged file to fall back on",
            )
            return
        if tk is None:
            self.db.update_track(
                t.id,
                status=dbmod.TRACK_FAILED,
                error="beets skipped import; no MB track mapping to tag with",
            )
            return
        try:
            final = mutagen_tagger.place(staged, self.config.music_dir, release, tk)
            self._copy_album_cover(final, album_id, skip=final)
            self._maybe_strip_featured(final)
            self.db.update_track(
                t.id, status=dbmod.TRACK_DONE, final_path=str(final), error=None
            )
            self._emit(t.id, 1.0, "done")
        except Exception as e:
            self.db.update_track(t.id, status=dbmod.TRACK_FAILED, error=str(e)[:500])

    def _copy_album_cover(self, dest: Path, album_id: int, skip: Path) -> None:
        """Copy embedded cover art from a done sibling track into ``dest``."""
        for sib in self.db.list_tracks(album_id):
            if sib.status != dbmod.TRACK_DONE or not sib.final_path:
                continue
            sp = Path(sib.final_path)
            if not sp.exists() or sp == skip:
                continue
            if mutagen_tagger.embed_cover_from(dest, sp):
                return

    def _emit(self, track_id: int, frac: float | None, status: str) -> None:
        if self.progress_cb:
            try:
                self.progress_cb(track_id, frac, status)
            except Exception:
                pass

    def _maybe_strip_featured(self, final: Path) -> None:
        """Strip "feat." guests from the final file's grouping tags, if enabled."""
        if self.config.strip_featured_artists:
            try:
                mutagen_tagger.normalize_featured_artists(final)
            except Exception:
                pass  # tagging is best-effort; never fail the track here

    def _normalize_album(self, album_id: int) -> None:
        """Write R128 track + album gain tags across a finished album.

        Only used on the mutagen path (beets computes its own via the plugin).
        Best-effort: a measurement or tag-write failure never fails the album.
        """
        try:
            paths = [
                Path(t.final_path)
                for t in self.db.list_tracks(album_id)
                if t.status == dbmod.TRACK_DONE and t.final_path
            ]
            tagged = replaygain.normalize_album(paths)
            if tagged:
                self._emit(0, None, f"replaygain: tagged {tagged} track(s)")
        except Exception as e:
            self._emit(0, None, f"replaygain skipped: {e}")


def _staged_name(track: Track, ext: str) -> str:
    return f"{track.disc}-{track.position:02d} {mutagen_tagger.safe(track.title)}{ext}"
