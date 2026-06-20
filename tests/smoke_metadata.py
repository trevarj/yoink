"""Live smoke test: MusicBrainz browse + ytmusicapi album/track lookup.

Run: PYTHONPATH=src .venv/bin/python tests/smoke_metadata.py
Hits the network. Verifies the two flagged risks: MB tracklist+durations, and
unauthenticated ytmusicapi get_album/audioPlaylistId.
"""

from __future__ import annotations

from dataclasses import replace

from yoink.config import load_config
from yoink.metadata.musicbrainz import MusicBrainz


def main() -> None:
    cfg = replace(load_config(), mb_contact="yoink-smoke-test (tmarjeski@gmail.com)")
    mb = MusicBrainz(cfg)

    print("=== MusicBrainz: search 'Daft Punk Discovery' ===")
    hits = mb.search_release_groups("artist:Daft Punk AND releasegroup:Discovery", limit=5)
    for h in hits[:5]:
        print(f"  RG {h.mbid[:8]}  {h.artist} - {h.title} ({h.primary_type}, {h.year})")
    assert hits, "no release-group hits"

    rg = hits[0]
    print(f"\n=== Canonical release for '{rg.title}' ===")
    release = mb.release_for_group(rg.mbid)
    assert release is not None, "no canonical release"
    print(f"  release MBID: {release.mbid}")
    print(f"  {release.artist} - {release.title} ({release.year}, {release.country}), "
          f"{release.track_count} tracks")
    for t in release.tracks[:4]:
        dur = f"{t.duration_ms / 1000:.0f}s" if t.duration_ms else "?"
        print(f"    {t.disc}.{t.position:02d}  {t.title}  [{dur}]")
    assert any(t.duration_ms for t in release.tracks), "no track durations"

    print("\n=== ytmusicapi: unauth album lookup ===")
    from ytmusicapi import YTMusic

    yt = YTMusic()
    album_hits = yt.search(f"{release.artist} {release.title}", filter="albums", limit=3)
    print(f"  {len(album_hits)} album hits")
    browse_id = None
    for a in album_hits[:3]:
        print(f"    {a.get('artists')} - {a.get('title')}  browseId={a.get('browseId')}")
        if browse_id is None:
            browse_id = a.get("browseId")
    assert browse_id, "no album browseId"

    album = yt.get_album(browse_id)
    apid = album.get("audioPlaylistId")
    print(f"  get_album OK: audioPlaylistId={apid}  tracks={len(album.get('tracks', []))}")
    assert apid, "no audioPlaylistId (album-as-playlist path unavailable)"

    print("\n=== ytmusicapi: per-track song search ===")
    t0 = release.tracks[0]
    songs = yt.search(f"{t0.artist} {t0.title}", filter="songs", limit=3)
    for s in songs[:3]:
        print(f"    {s.get('resultType')}  {s.get('artists')} - {s.get('title')}  "
              f"dur={s.get('duration')}  videoId={s.get('videoId')}")
    assert songs, "no song hits"

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
