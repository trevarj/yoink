"""Integration test for the beets backend (real `beet import` subprocess).

Generates a full album of correct-duration silent opus files, pre-tags them
like the worker does, then runs an isolated non-interactive import and checks
files land in the library tree. Run inside a shell with ffmpeg + beet:

  guix shell ffmpeg -- env PYTHONPATH=src .venv/bin/python tests/smoke_beets.py
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from yoink.config import load_config
from yoink.metadata.musicbrainz import MusicBrainz
from yoink.tagging import mutagen_tagger
from yoink.tagging.beets_tagger import BeetsTagger

DISCOVERY_RELEASE = "6cd30d99-4923-4d5e-8e51-9d87506976f1"


def silent_opus(path: Path, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-t", f"{max(1.0, seconds):.0f}", "-c:a", "libopus", str(path)],
        check=True, capture_output=True,
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="yoink-beets-"))
    base = load_config()
    cfg = replace(
        base, state_dir=tmp / "state", cache_dir=base.cache_dir,
        music_dir=tmp / "music", tagger="beets",
        mb_contact="yoink-smoke (tmarjeski@gmail.com)",
    )
    cfg.ensure_dirs()

    release = MusicBrainz(cfg).get_release(DISCOVERY_RELEASE)
    album_dir = cfg.staging_dir / "album_test"
    album_dir.mkdir(parents=True, exist_ok=True)

    for t in release.tracks:
        f = album_dir / f"{t.disc}-{t.position:02d} {mutagen_tagger.safe(t.title)}.opus"
        silent_opus(f, (t.duration_ms or 2000) / 1000.0)
        mutagen_tagger.write_tags(f, release, t)
    print(f"staged {len(release.tracks)} pre-tagged files")

    tagger = BeetsTagger(cfg)
    out = tagger.import_album(album_dir, release.mbid)
    print("beet output (tail):")
    print("\n".join(out.splitlines()[-6:]) or "  <empty>")

    imported = sorted((cfg.music_dir).rglob("*.opus"))
    print(f"\nimported {len(imported)} files into library:")
    for p in imported[:4]:
        print("  ", p.relative_to(cfg.music_dir))

    assert imported, "beets imported no files (match rejected?)"
    assert len(imported) == release.track_count, (
        f"expected {release.track_count}, got {len(imported)}"
    )
    # Confirm the path format Artist/Album/NN Title.
    sample = imported[0].relative_to(cfg.music_dir)
    assert sample.parts[0] == "Daft Punk" and sample.parts[1] == "Discovery", sample
    print("\nBEETS INTEGRATION PASSED")


if __name__ == "__main__":
    main()
