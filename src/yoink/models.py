"""Shared data models passed between the metadata, queue, and download layers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Track:
    position: int  # track number within its disc
    disc: int
    title: str
    artist: str
    duration_ms: int | None  # canonical length from MusicBrainz, used for matching
    recording_mbid: str | None = None


@dataclass(frozen=True)
class Release:
    """A specific MusicBrainz release (the thing we tag against)."""

    mbid: str
    title: str
    artist: str
    artist_mbid: str | None
    date: str | None
    year: int | None
    country: str | None
    track_count: int
    tracks: tuple[Track, ...]


@dataclass(frozen=True)
class ReleaseGroupHit:
    """A search result: an album abstractly, before picking a concrete release."""

    mbid: str  # release-group MBID
    title: str
    artist: str
    artist_mbid: str | None
    primary_type: str | None
    year: int | None
    disambiguation: str | None = None
    secondary_types: tuple[str, ...] = ()

    @property
    def description(self) -> str:
        """Human note for the browse list: disambiguation + secondary types."""
        bits = list(self.secondary_types)
        if self.disambiguation:
            bits.append(self.disambiguation)
        return " · ".join(bits)


@dataclass(frozen=True)
class ArtistHit:
    mbid: str
    name: str
    disambiguation: str | None
    country: str | None
