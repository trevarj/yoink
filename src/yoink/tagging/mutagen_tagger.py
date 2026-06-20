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

import mutagen
from mutagen.flac import Picture
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from ..models import Release, Track

_RESERVED = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


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


def write_tags(path: Path, release: Release, track: Track, art: bytes | None = None) -> None:
    """Overwrite the file's tags with authoritative MB metadata."""
    suffix = path.suffix.lower()
    if suffix in (".opus", ".ogg"):
        audio = OggOpus(path) if suffix == ".opus" else OggVorbis(path)
        for k, v in _vorbis_tags(release, track).items():
            audio[k] = v
        if art:
            _embed_vorbis_art(audio, art, "image/jpeg")
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
        if art:
            audio["covr"] = [MP4Cover(art, imageformat=MP4Cover.FORMAT_JPEG)]
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
    art: bytes | None = None,
) -> Path:
    """Tag the staged file and move it to its final library path."""
    write_tags(staged, release, track, art)
    dest = final_path(music_dir, release, track, staged.suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), str(dest))
    return dest
