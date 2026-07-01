"""Direct tagging with mutagen using the known MusicBrainz metadata.

Because each staged file was downloaded for a specific MB track, the mapping is
exact -- no guessing. This writes canonical tags, optionally embeds cover art,
and moves the file into ``<music_dir>/<albumartist>/<album>/NN Title.ext``.

Used as the standalone tagger when ``tagger = "mutagen"``, and to pre-tag files
before a beets import so beets' assignment is unambiguous.
"""

from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path
from typing import Any

import mutagen
from mutagen.flac import Picture
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from ..models import Release, Track

_RESERVED = re.compile(r'[/\\:*?"<>|\x00-\x1f]')

# A featured-guest join phrase, e.g. "Boys of Fall feat. Joey Fleming". The
# separator must have whitespace on both sides so words like "Defeat" don't
# match. Strips everything from the first feature marker onward.
_FEATURE_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)

# Single-string fields a player groups on; the multi-value credit lists
# (artists, artists_credit) are left alone so the guest stays credited.
_GROUPING_FIELDS = ("artist", "artist_credit", "artistsort")


def strip_featured(value: str) -> str:
    """Drop the featured-guest portion: "A feat. B" -> "A", "A ft. B" -> "A".

    Collaborations joined by " & " are preserved.
    """
    return _FEATURE_RE.sub("", value).strip()


def safe(name: str, fallback: str = "Unknown") -> str:
    cleaned = _RESERVED.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:180] or fallback


def _multi_disc(release: Release) -> bool:
    return any(t.disc > 1 for t in release.tracks)


def final_path(music_dir: Path, release: Release, track: Track, ext: str) -> Path:
    artist = safe(release.artist, "Unknown Artist")
    album = safe(release.title, "Unknown Album")
    if _multi_disc(release):
        stem = f"{track.disc}-{track.position:02d} {safe(track.title)}"
    else:
        stem = f"{track.position:02d} {safe(track.title)}"
    return music_dir / artist / album / f"{stem}.{ext.lstrip('.')}"


def _vorbis_tags(release: Release, track: Track) -> dict[str, str]:
    tags = {
        "title": track.title,
        "artist": track.artist,
        "albumartist": release.artist,
        "album": release.title,
        "tracknumber": str(track.position),
        "tracktotal": str(release.track_count),
        "discnumber": str(track.disc),
    }
    if release.date:
        tags["date"] = release.date
    if release.mbid:
        tags["musicbrainz_albumid"] = release.mbid
    if track.recording_mbid:
        tags["musicbrainz_trackid"] = track.recording_mbid
    if release.artist_mbid:
        tags["musicbrainz_albumartistid"] = release.artist_mbid
    return tags


def _embed_vorbis_art(audio, art: bytes, mime: str) -> None:
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = mime
    pic.data = art
    audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]


def _art_payload(art: Any) -> tuple[bytes, str] | None:
    if art is None:
        return None
    if isinstance(art, bytes):
        return art, "image/jpeg"
    data = getattr(art, "data", None)
    mime = getattr(art, "mime", None)
    if isinstance(data, bytes) and isinstance(mime, str):
        return data, mime
    raise TypeError("art must be bytes or an object with data and mime fields")


def _mp4_cover_format(mime: str) -> int | None:
    if mime == "image/png":
        return MP4Cover.FORMAT_PNG
    if mime in ("image/jpeg", "image/jpg"):
        return MP4Cover.FORMAT_JPEG
    return None


def write_tags(path: Path, release: Release, track: Track, art: Any = None) -> None:
    """Overwrite the file's tags with authoritative MB metadata."""
    cover = _art_payload(art)
    suffix = path.suffix.lower()
    if suffix in (".opus", ".ogg"):
        audio = OggOpus(path) if suffix == ".opus" else OggVorbis(path)
        for k, v in _vorbis_tags(release, track).items():
            audio[k] = v
        if cover:
            _embed_vorbis_art(audio, cover[0], cover[1])
        audio.save()
    elif suffix in (".m4a", ".mp4", ".aac"):
        audio = MP4(path)
        m = _vorbis_tags(release, track)
        audio["\xa9nam"] = m["title"]
        audio["\xa9ART"] = m["artist"]
        audio["aART"] = m["albumartist"]
        audio["\xa9alb"] = m["album"]
        audio["trkn"] = [(track.position, release.track_count)]
        audio["disk"] = [(track.disc, 0)]
        if release.date:
            audio["\xa9day"] = release.date
        if cover:
            imageformat = _mp4_cover_format(cover[1])
            if imageformat is not None:
                audio["covr"] = [MP4Cover(cover[0], imageformat=imageformat)]
        audio.save()
    else:
        # Best-effort generic write (mp3 etc.) via mutagen's easy interface.
        audio = mutagen.File(path, easy=True)
        if audio is None:
            raise ValueError(f"unsupported audio file for tagging: {path}")
        for k, v in _vorbis_tags(release, track).items():
            try:
                audio[k] = v
            except (KeyError, ValueError):
                pass
        audio.save()


def place(
    staged: Path,
    music_dir: Path,
    release: Release,
    track: Track,
    art: Any = None,
) -> Path:
    """Tag the staged file and move it to its final library path."""
    write_tags(staged, release, track, art)
    dest = final_path(music_dir, release, track, staged.suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), str(dest))
    return dest


def normalize_featured_artists(path: Path) -> None:
    """Strip featured-guest artists from the track's grouping tag fields in place.

    A track credited "A feat. B" gets its ``artist``/``artist_credit``/
    ``artistsort`` set to "A" so a featured single doesn't split from its album
    in a player. The multi-value credit lists (``artists``, ``artists_credit``)
    and the album artist are untouched, so the guest stays credited. Best-effort:
    missing tags or an unreadable file are silently skipped.
    """
    try:
        audio = mutagen.File(path)
    except Exception:
        return
    if audio is None or audio.tags is None:
        return
    changed = False
    for key in _GROUPING_FIELDS:
        values = audio.tags.get(key)
        if not values:
            continue
        cleaned = [strip_featured(v) for v in values if isinstance(v, str)]
        cleaned = [c for c in cleaned if c]
        if cleaned and cleaned != list(values):
            audio.tags[key] = cleaned
            changed = True
    if changed:
        audio.save()


def embed_cover_from(dest: Path, src: Path) -> bool:
    """Copy the embedded cover art tag from ``src`` to ``dest`` (best-effort).

    Used when beets skips importing a track and we fall back to mutagen tagging,
    so the fallback file gets the same cover art as its siblings. Returns True if
    a cover was copied.
    """
    try:
        source = mutagen.File(src)
        target = mutagen.File(dest)
    except Exception:
        return False
    if source is None or target is None or source.tags is None or target.tags is None:
        return False
    picture = source.tags.get("metadata_block_picture")
    if not picture:
        return False
    try:
        target.tags["metadata_block_picture"] = picture
        target.save()
        return True
    except Exception:
        return False
