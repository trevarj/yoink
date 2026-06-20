"""YouTube Music search via ytmusicapi (unauthenticated).

Two discovery strategies, used by the worker in order:

1. Album-as-playlist: map a MusicBrainz release to a YTM album by artist +
   title + track count, then read its ``audioPlaylistId`` and per-track
   ``videoId`` list. Coherent mastering and order, fewest requests.
2. Per-track search: fall back to searching each track individually; the
   :mod:`yoink.youtube.matcher` scores the candidates.

``get_album``/``audioPlaylistId`` were verified to work unauthenticated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz
from ytmusicapi import YTMusic

_VIDEO_ID = r"[A-Za-z0-9_-]{11}"


def parse_video_id(text: str) -> str | None:
    """Extract a YouTube videoId from a watch/short URL or a raw id."""
    text = text.strip()
    m = re.search(rf"[?&]v=({_VIDEO_ID})", text)
    if m:
        return m.group(1)
    m = re.search(rf"(?:youtu\.be/|/)({_VIDEO_ID})(?:[?&/]|$)", text)
    if m:
        return m.group(1)
    if re.fullmatch(_VIDEO_ID, text):
        return text
    return None


def _join_artists(entry: dict) -> str:
    artists = entry.get("artists") or []
    names = [a.get("name", "") for a in artists if isinstance(a, dict)]
    return ", ".join(n for n in names if n)


def _duration_seconds(entry: dict) -> int | None:
    secs = entry.get("duration_seconds")
    if isinstance(secs, int) and secs > 0:
        return secs
    text = entry.get("duration")
    if not text:
        return None
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    total = 0
    for n in nums:
        total = total * 60 + n
    return total or None


@dataclass(frozen=True)
class Candidate:
    """A normalized YouTube Music search result."""

    video_id: str
    title: str
    artist: str
    album: str | None
    duration_s: int | None
    result_type: str  # 'song' | 'video' | ...


def _to_candidate(entry: dict) -> Candidate | None:
    vid = entry.get("videoId")
    if not vid:
        return None
    album = entry.get("album")
    album_name = album.get("name") if isinstance(album, dict) else None
    return Candidate(
        video_id=vid,
        title=entry.get("title", ""),
        artist=_join_artists(entry),
        album=album_name,
        duration_s=_duration_seconds(entry),
        result_type=entry.get("resultType", ""),
    )


@dataclass(frozen=True)
class AlbumMatch:
    browse_id: str
    audio_playlist_id: str
    title: str
    artist: str
    track_video_ids: tuple[str, ...]
    track_titles: tuple[str, ...]


class YouTubeMusic:
    def __init__(self) -> None:
        self._yt: YTMusic | None = None

    @property
    def yt(self) -> YTMusic:
        if self._yt is None:
            self._yt = YTMusic()  # unauthenticated
        return self._yt

    # --- album-as-playlist -------------------------------------------------
    def find_album(
        self, artist: str, title: str, track_count: int, min_title_ratio: float = 80.0
    ) -> AlbumMatch | None:
        """Find a confident YTM album for an MB release, or None.

        Confidence requires a close artist+title match and an exact track-count
        match (guards against deluxe/standard edition mix-ups).
        """
        results = self.yt.search(f"{artist} {title}", filter="albums", limit=5)
        for r in results:
            browse_id = r.get("browseId")
            if not browse_id:
                continue
            if fuzz.token_set_ratio(title, r.get("title", "")) < min_title_ratio:
                continue
            if fuzz.token_set_ratio(artist, _join_artists(r)) < 70:
                continue
            album = self.yt.get_album(browse_id)
            tracks = album.get("tracks") or []
            apid = album.get("audioPlaylistId")
            if not apid:
                continue
            if track_count and len(tracks) != track_count:
                continue  # edition mismatch -> not confident
            vids, titles = [], []
            for t in tracks:
                vids.append(t.get("videoId") or "")
                titles.append(t.get("title") or "")
            return AlbumMatch(
                browse_id=browse_id,
                audio_playlist_id=apid,
                title=album.get("title", title),
                artist=artist,
                track_video_ids=tuple(vids),
                track_titles=tuple(titles),
            )
        return None

    # --- per-track search --------------------------------------------------
    def search_track(self, artist: str, title: str, limit: int = 6) -> list[Candidate]:
        query = f"{artist} {title}".strip()
        results = self.yt.search(query, filter="songs", limit=limit)
        candidates = [c for c in (_to_candidate(r) for r in results) if c is not None]
        if candidates:
            return candidates
        # Fall back to an unfiltered search (covers tracks only on video).
        results = self.yt.search(query, limit=limit)
        return [c for c in (_to_candidate(r) for r in results) if c is not None]
