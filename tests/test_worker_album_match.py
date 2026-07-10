"""Duration validation for album-aligned YouTube Music matches."""

from __future__ import annotations

from dataclasses import replace

from yoink.config import Config
from yoink.jobs.db import Database
from yoink.jobs.worker import Worker
from yoink.models import Track
from yoink.youtube.search import AlbumMatch, YouTubeMusic

VID = "AAAAAAAAAAA"
TRACK = Track(1, 1, "One More Time", "Daft Punk", 320_000, "rec1")


def _worker(tmp_path) -> Worker:
    cfg = replace(Config(state_dir=tmp_path), duration_gate_s=3.0)
    cfg.ensure_dirs()
    return Worker(cfg, Database(cfg.db_path))


def _album_match(duration_s: int | None) -> AlbumMatch:
    return AlbumMatch(
        browse_id="MPREb_album",
        audio_playlist_id="OLAK_playlist",
        title="Discovery",
        artist="Daft Punk",
        track_video_ids=(VID,),
        track_titles=(TRACK.title,),
        track_durations_s=(duration_s,),
    )


def test_album_aligned_match_inside_duration_gate_is_accepted(tmp_path):
    worker = _worker(tmp_path)
    worker._yt.search_track = lambda artist, title: []  # type: ignore[method-assign]

    assert worker._resolve_video_id(TRACK, _album_match(322), 0) == (
        VID,
        100.0,
        "album-aligned",
    )


def test_album_aligned_match_outside_duration_gate_falls_back(tmp_path):
    worker = _worker(tmp_path)
    searches = []

    def search_track(artist, title):
        searches.append((artist, title))
        return []

    worker._yt.search_track = search_track  # type: ignore[method-assign]

    video_id, _score, reason = worker._resolve_video_id(
        TRACK, _album_match(330), 0
    )

    assert video_id is None
    assert reason == "no candidates"
    assert searches == [(TRACK.artist, TRACK.title)]


def test_find_album_preserves_track_durations_for_worker_gate():
    class FakeYT:
        def search(self, query, filter, limit):
            return [
                {
                    "browseId": "MPREb_album",
                    "title": "Discovery",
                    "artists": [{"name": "Daft Punk"}],
                }
            ]

        def get_album(self, browse_id):
            return {
                "title": "Discovery",
                "audioPlaylistId": "OLAK_playlist",
                "tracks": [
                    {"videoId": VID, "title": TRACK.title, "duration": "5:20"}
                ],
            }

    youtube = YouTubeMusic()
    youtube._yt = FakeYT()  # type: ignore[assignment]

    match = youtube.find_album("Daft Punk", "Discovery", 1)

    assert match is not None
    assert match.track_durations_s == (320,)
