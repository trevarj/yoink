"""MusicBrainz metadata provider.

Browse path: search release-groups (or artists) -> list a release-group's
releases -> pick a canonical release -> fetch its full tracklist with per-track
durations and the release MBID (which feeds beets at tag time).

MusicBrainz allows ~1 req/s and requires a descriptive User-Agent. We rely on
musicbrainzngs' built-in rate limiter and layer an indefinite on-disk cache on
top so repeat browsing is instant and stays under the limit.
"""

from __future__ import annotations

import re

import musicbrainzngs

from ..config import Config
from ..models import ArtistHit, Release, ReleaseGroupHit, Track
from .cache import JsonCache

# Prefer these release countries when choosing a canonical release.
_COUNTRY_PREFERENCE = ("XW", "US", "GB", "XE", "DE")

# Lucene special characters that must be escaped in a term.
_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/&|])')
# Detect when a user typed a real Lucene query we should pass through verbatim.
_FIELD_QUERY = re.compile(
    r"\b(artist|artistname|releasegroup|release|arid|reid|tag|type|"
    r"primarytype|secondarytype|status|comment|creditname)\s*:"
)
_BOOLEAN = re.compile(r"\s(AND|OR|NOT)\s")
# Separators a user might put between artist and album.
_SEPARATORS = (" - ", " – ", " — ", " / ")


def _lucene_escape(term: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", term)


def build_release_query(text: str) -> str:
    """Turn free user input into a robust release-group Lucene query.

    - Power users typing ``artist:… releasegroup:…`` (or AND/OR/NOT) pass
      through untouched.
    - ``Artist - Album`` is split into required artist + release-group terms.
    - Otherwise each word must appear in EITHER the artist or the title, so the
      artist/album split and word order don't matter and unrelated results
      (terms only in the title) get filtered out.
    """
    text = text.strip()
    if not text or _FIELD_QUERY.search(text) or _BOOLEAN.search(text):
        return text

    for sep in _SEPARATORS:
        if sep in text:
            left, right = (p.strip() for p in text.split(sep, 1))
            if left and right:
                clauses = [f"+artist:{_lucene_escape(t)}" for t in left.split()]
                clauses += [f"+releasegroup:{_lucene_escape(t)}" for t in right.split()]
                return " ".join(clauses)

    clauses = [
        f"+(artist:{_lucene_escape(t)} OR releasegroup:{_lucene_escape(t)})"
        for t in text.split()
    ]
    return " ".join(clauses)


def _year_from_date(date: str | None) -> int | None:
    if not date:
        return None
    head = date.split("-", 1)[0]
    return int(head) if head.isdigit() else None


class MusicBrainz:
    def __init__(self, config: Config) -> None:
        self._cache = JsonCache(config.mb_cache_dir)
        # musicbrainzngs wants app name/version/contact split out.
        musicbrainzngs.set_useragent("yoink", "0.1.0", config.mb_contact)
        # Enforce the 1 req/s courtesy limit at the library level.
        musicbrainzngs.set_rate_limit(limit_or_interval=1.0, new_requests=1)

    # --- low-level cached calls -------------------------------------------
    def _cached(self, key: str, fn):
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        value = fn()
        self._cache.set(key, value)
        return value

    # --- search ------------------------------------------------------------
    def search_albums(self, text: str, limit: int = 25) -> list[ReleaseGroupHit]:
        """Search from free user input via a robust cross-field query."""
        return self.search_release_groups(build_release_query(text), limit=limit)

    def search_release_groups(self, query: str, limit: int = 25) -> list[ReleaseGroupHit]:
        key = f"rg-search:{limit}:{query.lower()}"
        data = self._cached(
            key,
            lambda: musicbrainzngs.search_release_groups(query=query, limit=limit),
        )
        hits: list[ReleaseGroupHit] = []
        for rg in data.get("release-group-list", []):
            credit = rg.get("artist-credit", [])
            artist, artist_mbid = _first_artist(credit)
            hits.append(
                ReleaseGroupHit(
                    mbid=rg["id"],
                    title=rg.get("title", "?"),
                    artist=artist,
                    artist_mbid=artist_mbid,
                    primary_type=rg.get("primary-type"),
                    year=_year_from_date(rg.get("first-release-date")),
                    disambiguation=rg.get("disambiguation") or None,
                    secondary_types=tuple(_secondary_types(rg)),
                )
            )
        return hits

    def search_artists(self, name: str, limit: int = 25) -> list[ArtistHit]:
        key = f"artist-search:{limit}:{name.lower()}"
        data = self._cached(
            key, lambda: musicbrainzngs.search_artists(artist=name, limit=limit)
        )
        return [
            ArtistHit(
                mbid=a["id"],
                name=a.get("name", "?"),
                disambiguation=a.get("disambiguation"),
                country=a.get("country"),
            )
            for a in data.get("artist-list", [])
        ]

    def artist_release_groups(self, artist_mbid: str) -> list[ReleaseGroupHit]:
        key = f"artist-rgs:{artist_mbid}"
        data = self._cached(
            key,
            lambda: musicbrainzngs.browse_release_groups(
                artist=artist_mbid, release_type=["album", "ep"], limit=100
            ),
        )
        hits: list[ReleaseGroupHit] = []
        for rg in data.get("release-group-list", []):
            hits.append(
                ReleaseGroupHit(
                    mbid=rg["id"],
                    title=rg.get("title", "?"),
                    artist="",  # known from context; filled by caller if needed
                    artist_mbid=artist_mbid,
                    primary_type=rg.get("primary-type"),
                    year=_year_from_date(rg.get("first-release-date")),
                    disambiguation=rg.get("disambiguation") or None,
                    secondary_types=tuple(_secondary_types(rg)),
                )
            )
        hits.sort(key=lambda h: (h.year or 9999, h.title))
        return hits

    # --- release selection + fetch ----------------------------------------
    def _release_group_releases(self, rg_mbid: str) -> list[dict]:
        key = f"rg-releases:{rg_mbid}"
        data = self._cached(
            key,
            lambda: musicbrainzngs.browse_releases(
                release_group=rg_mbid,
                includes=["media"],
                limit=100,
            ),
        )
        return data.get("release-list", [])

    def pick_canonical_release(self, rg_mbid: str) -> str | None:
        """Choose the most representative release id for a release-group.

        Prefer Official status, an earliest date, and a preferred country. This
        avoids deluxe/bonus editions when a plain album release exists.
        """
        releases = self._release_group_releases(rg_mbid)
        if not releases:
            return None

        def score(r: dict) -> tuple:
            status = (r.get("status") or "").lower()
            official = 0 if status == "official" else 1
            year = _year_from_date(r.get("date")) or 9999
            country = r.get("country") or ""
            country_rank = (
                _COUNTRY_PREFERENCE.index(country)
                if country in _COUNTRY_PREFERENCE
                else len(_COUNTRY_PREFERENCE)
            )
            track_count = _release_track_count(r)
            return (official, year, country_rank, track_count)

        return min(releases, key=score)["id"]

    def get_release(self, release_mbid: str) -> Release:
        key = f"release:{release_mbid}"
        data = self._cached(
            key,
            lambda: musicbrainzngs.get_release_by_id(
                release_mbid,
                includes=["recordings", "artist-credits", "media"],
            ),
        )
        rel = data["release"]
        artist, artist_mbid = _first_artist(rel.get("artist-credit", []))
        tracks: list[Track] = []
        for disc_no, medium in enumerate(rel.get("medium-list", []), start=1):
            for t in medium.get("track-list", []):
                rec = t.get("recording", {})
                length = t.get("length") or rec.get("length")
                t_artist, _ = _first_artist(rec.get("artist-credit", []))
                tracks.append(
                    Track(
                        position=int(t.get("position", t.get("number", 0)) or 0),
                        disc=disc_no,
                        title=t.get("title") or rec.get("title", "?"),
                        artist=t_artist or artist,
                        duration_ms=int(length) if length else None,
                        recording_mbid=rec.get("id"),
                    )
                )
        return Release(
            mbid=rel["id"],
            title=rel.get("title", "?"),
            artist=artist,
            artist_mbid=artist_mbid,
            date=rel.get("date"),
            year=_year_from_date(rel.get("date")),
            country=rel.get("country"),
            track_count=len(tracks),
            tracks=tuple(tracks),
        )

    def release_for_group(self, rg_mbid: str) -> Release | None:
        """Convenience: pick the canonical release of a group and fetch it."""
        release_id = self.pick_canonical_release(rg_mbid)
        if release_id is None:
            return None
        return self.get_release(release_id)


def _secondary_types(rg: dict) -> list[str]:
    """Read a release-group's secondary types (Live, Compilation, …)."""
    types = rg.get("secondary-type-list") or rg.get("secondary-types") or []
    return [t for t in types if isinstance(t, str)]


def _first_artist(credit: list) -> tuple[str, str | None]:
    """Flatten a MusicBrainz artist-credit list into 'A & B' plus first MBID."""
    if not credit:
        return "", None
    parts: list[str] = []
    first_mbid: str | None = None
    for entry in credit:
        if isinstance(entry, str):
            parts.append(entry)  # join phrase like " & "
        elif isinstance(entry, dict):
            artist = entry.get("artist", {})
            parts.append(artist.get("name", entry.get("name", "")))
            if first_mbid is None:
                first_mbid = artist.get("id")
    return "".join(parts).strip(), first_mbid


def _release_track_count(release: dict) -> int:
    total = 0
    for medium in release.get("medium-list", []):
        tc = medium.get("track-count")
        if tc is not None:
            total += int(tc)
    return total
