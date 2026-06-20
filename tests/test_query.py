"""Pure unit tests for the MusicBrainz query builder."""

from __future__ import annotations

from yoink.metadata.musicbrainz import build_release_query


def test_plain_terms_are_cross_field_and_required():
    q = build_release_query("daft punk discovery")
    assert q == (
        "+(artist:daft OR releasegroup:daft) "
        "+(artist:punk OR releasegroup:punk) "
        "+(artist:discovery OR releasegroup:discovery)"
    )


def test_artist_album_separator_split():
    q = build_release_query("Massive Attack - Mezzanine")
    assert q == "+artist:Massive +artist:Attack +releasegroup:Mezzanine"


def test_field_query_passes_through():
    raw = "artist:Radiohead AND releasegroup:Kid A"
    assert build_release_query(raw) == raw


def test_boolean_passes_through():
    raw = "Discovery OR Homework"
    assert build_release_query(raw) == raw


def test_special_chars_escaped():
    q = build_release_query("AC/DC")
    assert "\\/" in q  # slash escaped, no Lucene syntax error


def test_empty():
    assert build_release_query("   ") == ""
