"""Pure unit tests for stripping featured-guest artists from tags (no audio I/O)."""

from __future__ import annotations

from pathlib import Path

from yoink.tagging import mutagen_tagger as mt
from yoink.tagging.mutagen_tagger import normalize_featured_artists, strip_featured


# --- strip_featured ---------------------------------------------------------
def test_strip_feat_dot():
    assert strip_featured("Boys of Fall feat. Joey Fleming") == "Boys of Fall"


def test_strip_ft_dot_and_featuring():
    assert strip_featured("A ft. B") == "A"
    assert strip_featured("A featuring B") == "A"
    assert strip_featured("A feat B") == "A"


def test_collab_amp_preserved():
    assert strip_featured("A & B") == "A & B"
    # Collaboration plus a guest: keep the collab, drop the guest.
    assert strip_featured("A & B feat. C") == "A & B"


def test_no_change_without_feature():
    assert strip_featured("Boys of Fall") == "Boys of Fall"
    assert strip_featured("") == ""


def test_word_boundary_not_substring():
    # "Defeat" must not be treated as a "feat" marker.
    assert strip_featured("Defeat the Beast") == "Defeat the Beast"


# --- normalize_featured_artists --------------------------------------------
class _Tags(dict):
    """Dict-of-lists stand-in for Vorbus comments."""


class _FakeAudio:
    def __init__(self, tags: dict) -> None:
        self.tags = _Tags(tags)
        self.saved = False

    def save(self) -> None:
        self.saved = True


def _patch(monkeypatch, tags: dict) -> _FakeAudio:
    fake = _FakeAudio(tags)
    monkeypatch.setattr(mt.mutagen, "File", lambda _p: fake)
    return fake


def test_normalize_strips_grouping_fields(tmp_path, monkeypatch):
    fake = _patch(monkeypatch, {
        "artist": ["Boys of Fall feat. Joey Fleming"],
        "artist_credit": ["Boys of Fall feat. Joey Fleming"],
        "artistsort": ["Boys of Fall feat. Fleming, Joey"],
        "artists": ["Boys of Fall", "Joey Fleming"],      # credit list -> kept
        "artists_credit": ["Boys of Fall", "Joey Fleming"],  # credit list -> kept
        "albumartist": ["Boys of Fall"],
    })
    normalize_featured_artists(Path(tmp_path / "x.opus"))
    assert fake.saved
    assert fake.tags["artist"] == ["Boys of Fall"]
    assert fake.tags["artist_credit"] == ["Boys of Fall"]
    assert fake.tags["artistsort"] == ["Boys of Fall"]
    # Multi-artist credits untouched: the guest is still credited.
    assert fake.tags["artists"] == ["Boys of Fall", "Joey Fleming"]
    assert fake.tags["artists_credit"] == ["Boys of Fall", "Joey Fleming"]
    assert fake.tags["albumartist"] == ["Boys of Fall"]


def test_normalize_no_change_no_save(tmp_path, monkeypatch):
    fake = _patch(monkeypatch, {"artist": ["Boys of Fall"], "albumartist": ["Boys of Fall"]})
    normalize_featured_artists(Path(tmp_path / "x.opus"))
    assert not fake.saved  # nothing to change -> no write


def test_normalize_missing_fields_ok(tmp_path, monkeypatch):
    fake = _patch(monkeypatch, {"albumartist": ["Boys of Fall"]})
    normalize_featured_artists(Path(tmp_path / "x.opus"))
    assert not fake.saved


def test_normalize_unreadable_file_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(mt.mutagen, "File", lambda _p: None)
    normalize_featured_artists(Path(tmp_path / "x.opus"))  # must not raise


def test_normalize_raises_silently_swallowed(tmp_path, monkeypatch):
    def boom(_p):
        raise OSError("disk")

    monkeypatch.setattr(mt.mutagen, "File", boom)
    normalize_featured_artists(Path(tmp_path / "x.opus"))  # must not raise