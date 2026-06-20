"""Score YouTube Music candidates against a known MusicBrainz track.

The goal is to never silently grab the wrong file: a track only matches if a
candidate clears a duration gate and a minimum score. Otherwise the worker
marks it ``needs_review``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from ..models import Track
from .search import Candidate

# Words that signal a wrong/alternate version. Penalized unless the word is
# genuinely part of the wanted title (e.g. a song literally called "Live").
_BAD_WORDS = (
    "live",
    "cover",
    "remix",
    "sped up",
    "slowed",
    "reverb",
    "karaoke",
    "instrumental",
    "8d",
    "nightcore",
    "extended",
    "mashup",
    "radio edit",
    "single version",
    "sped-up",
)


def _has_bad_word(text: str, wanted_title: str) -> bool:
    low = text.lower()
    wanted = wanted_title.lower()
    for w in _BAD_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low) and w not in wanted:
            return True
    return False


@dataclass(frozen=True)
class MatchResult:
    candidate: Candidate | None
    score: float
    accepted: bool
    reason: str


def _score(
    track: Track,
    cand: Candidate,
    gate_s: float,
    soft_s: float,
) -> tuple[float, bool]:
    """Return (score, within_hard_gate). Score is comparable across candidates."""
    score = 0.0
    within_gate = True

    # Duration: hard gate then graded closeness.
    if track.duration_ms and cand.duration_s:
        delta = abs(cand.duration_s - track.duration_ms / 1000.0)
        if delta <= gate_s:
            score += 10.0 - delta
        elif delta <= soft_s:
            within_gate = False
            score += max(0.0, 6.0 - delta)  # soft, penalized
        else:
            return (-100.0, False)  # outside soft window -> disqualified
    # If either side lacks duration we can't gate on it; lean on title/artist.

    # Channel/source quality.
    if cand.result_type == "song":
        score += 5.0  # auto-generated official audio / "Topic"
    elif cand.result_type == "video":
        score -= 4.0  # music videos drift in length / add intros

    # Title similarity: blend token_set (robust to word order / "feat." noise)
    # with plain ratio (rewards exactness, penalizing extra "(Radio Edit)"-style
    # qualifiers that token_set ignores because the wanted title is a subset).
    title_set = fuzz.token_set_ratio(track.title, cand.title)
    title_exact = fuzz.ratio(track.title.lower(), cand.title.lower())
    score += (0.6 * title_set + 0.4 * title_exact) / 100.0 * 8.0
    score += fuzz.token_set_ratio(track.artist, cand.artist) / 100.0 * 4.0

    # Bad-version penalty.
    if _has_bad_word(cand.title, track.title):
        score -= 6.0

    return (score, within_gate)


def score_candidates(
    track: Track,
    candidates: list[Candidate],
    *,
    gate_s: float,
    soft_s: float,
) -> list[tuple[Candidate, float]]:
    """Score every candidate against the track, best first (for manual review)."""
    scored = [(c, _score(track, c, gate_s, soft_s)[0]) for c in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def best_match(
    track: Track,
    candidates: list[Candidate],
    *,
    gate_s: float,
    soft_s: float,
    min_score: float,
) -> MatchResult:
    if not candidates:
        return MatchResult(None, 0.0, False, "no candidates")

    scored = [(c, *_score(track, c, gate_s, soft_s)) for c in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    best, score, within_gate = scored[0]

    if score < min_score:
        return MatchResult(best, score, False, f"below threshold ({score:.1f})")
    reason = "matched" if within_gate else "matched (soft duration)"
    return MatchResult(best, score, True, reason)
