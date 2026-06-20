"""Pure unit tests for filename sanitization and final-path layout."""

from __future__ import annotations

from pathlib import Path

from yoink.models import Release, Track
from yoink.tagging.mutagen_tagger import final_path, safe


def _release(tracks):
    return Release(
        mbid="r", title="Discovery", artist="Daft Punk", artist_mbid="a",
        date="2001", year=2001, country="XW", track_count=len(tracks), tracks=tuple(tracks),
    )


def test_safe_strips_path_separators():
    assert "/" not in safe("AC/DC")
    assert safe("   ") == "Unknown"
    assert safe("a" * 300) == "a" * 180


def test_single_disc_path():
    t = Track(1, 1, "One More Time", "Daft Punk", 320000)
    rel = _release([t])
    p = final_path(Path("/music"), rel, t, ".opus")
    assert p == Path("/music/Daft Punk/Discovery/01 One More Time.opus")


def test_multi_disc_path_has_disc_prefix():
    t1 = Track(1, 1, "A", "x", 1000)
    t2 = Track(1, 2, "B", "x", 1000)  # disc 2
    rel = _release([t1, t2])
    p = final_path(Path("/music"), rel, t2, ".opus")
    assert p.name == "2-01 B.opus"


def test_reserved_chars_in_title():
    t = Track(3, 1, 'Song: A/B?', "x", 1000)
    rel = _release([t])
    p = final_path(Path("/m"), rel, t, ".opus")
    assert ":" not in p.name and "/" not in p.name[:-5]
