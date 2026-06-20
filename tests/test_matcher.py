"""Pure unit tests for the matcher scoring (no network)."""

from __future__ import annotations

from yoink.models import Track
from yoink.youtube.matcher import best_match
from yoink.youtube.search import Candidate

TRACK = Track(position=1, disc=1, title="One More Time", artist="Daft Punk", duration_ms=320000)


def c(title, dur, rtype="song", vid="v", artist="Daft Punk", album="Discovery"):
    return Candidate(vid, title, artist, album, dur, rtype)


def match(cands):
    return best_match(TRACK, cands, gate_s=3.0, soft_s=7.0, min_score=6.0)


def test_exact_beats_qualifier_variant_at_same_duration():
    cands = [
        c("One More Time (Radio Edit)", 321, vid="bad"),
        c("One More Time", 321, vid="good"),
    ]
    r = match(cands)
    assert r.accepted
    assert r.candidate.video_id == "good"


def test_duration_gate_disqualifies_far_candidate():
    # Only a wildly-off-duration candidate -> rejected (needs review).
    r = match([c("One More Time", 120, vid="short")])
    assert not r.accepted


def test_live_version_penalized():
    cands = [
        c("One More Time (Live)", 320, vid="live"),
        c("One More Time", 320, vid="studio"),
    ]
    r = match(cands)
    assert r.candidate.video_id == "studio"


def test_music_video_loses_to_song():
    cands = [
        c("One More Time", 320, rtype="video", vid="mv"),
        c("One More Time", 320, rtype="song", vid="audio"),
    ]
    r = match(cands)
    assert r.candidate.video_id == "audio"


def test_empty_candidates_not_accepted():
    r = match([])
    assert not r.accepted and r.candidate is None
