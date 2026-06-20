"""Pure unit tests for videoId parsing."""

from __future__ import annotations

import pytest

from yoink.youtube.search import parse_video_id


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://music.youtube.com/watch?v=fa5IWHDbftI", "fa5IWHDbftI"),
        ("https://www.youtube.com/watch?v=fa5IWHDbftI&list=xyz", "fa5IWHDbftI"),
        ("https://youtu.be/fa5IWHDbftI", "fa5IWHDbftI"),
        ("https://youtu.be/fa5IWHDbftI?t=30", "fa5IWHDbftI"),
        ("fa5IWHDbftI", "fa5IWHDbftI"),
        ("  fa5IWHDbftI  ", "fa5IWHDbftI"),
    ],
)
def test_parse_video_id_ok(text, expected):
    assert parse_video_id(text) == expected


@pytest.mark.parametrize("text", ["", "not a url", "https://example.com/", "short"])
def test_parse_video_id_rejects(text):
    assert parse_video_id(text) is None
