"""End-to-end worker smoke test with a mocked downloader.

Exercises the real chain -- claim -> get_release (cached) -> album match ->
resolve videoId -> (mock download) -> mutagen tag + place -> DB transitions --
without pulling large audio. Run inside a shell with ffmpeg available:

  guix shell ffmpeg -- env PYTHONPATH=src .venv/bin/python tests/smoke_worker.py
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from yoink.config import load_config
from yoink.jobs import db as dbmod
from yoink.jobs.db import Database
from yoink.jobs.worker import Worker
from yoink.metadata.musicbrainz import MusicBrainz

DISCOVERY_RELEASE = "6cd30d99-4923-4d5e-8e51-9d87506976f1"


def make_template(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-t", "1", "-c:a", "libopus", str(path)],
        check=True, capture_output=True,
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="yoink-e2e-"))
    cfg = replace(
        load_config(),
        state_dir=tmp / "state",
        cache_dir=load_config().cache_dir,  # reuse real MB cache
        music_dir=tmp / "music",
        mb_contact="yoink-smoke (tmarjeski@gmail.com)",
        tagger="mutagen",
    )
    cfg.ensure_dirs()

    template = tmp / "template.opus"
    make_template(template)

    mb = MusicBrainz(cfg)
    full = mb.get_release(DISCOVERY_RELEASE)
    # Enqueue only the first 2 tracks so the test stays small; the worker still
    # re-reads the full release and matches these via the album path.
    trimmed = replace(full, tracks=full.tracks[:2], track_count=2)

    db = Database(cfg.db_path)
    album_id = db.enqueue_release(trimmed)
    print("enqueued album", album_id, "with", len(trimmed.tracks), "tracks")

    worker = Worker(cfg, db)

    # Mock the network download: copy the template to staging/<id>.opus.
    def fake_download(video_id, progress_cb=None):
        dest = cfg.staging_dir / f"{video_id}.opus"
        shutil.copy(template, dest)
        if progress_cb:
            progress_cb(1.0, "processing")
        return dest

    worker._dl.download = fake_download  # type: ignore[method-assign]

    worker.start()
    deadline = time.time() + 60
    while time.time() < deadline:
        album = db.get_album_job(album_id)
        if album and album.status in (dbmod.ALBUM_DONE, dbmod.ALBUM_FAILED):
            break
        time.sleep(0.5)
    worker.stop()
    worker.join(timeout=5)

    print("\n=== result ===")
    album = db.get_album_job(album_id)
    print("album status:", album.status)
    import mutagen
    for t in db.list_tracks(album_id):
        print(f"  [{t.status}] {t.track_no:02d} {t.title}  vid={t.yt_video_id} "
              f"score={t.match_score}")
        if t.final_path:
            p = Path(t.final_path)
            assert p.exists(), f"missing final file {p}"
            f = mutagen.File(p)
            print(f"      -> {p.relative_to(cfg.music_dir)}  album={f.get('album')} "
                  f"track={f.get('tracknumber')}")

    done = [t for t in db.list_tracks(album_id) if t.status == dbmod.TRACK_DONE]
    assert album.status == dbmod.ALBUM_DONE, f"album not done: {album.status}"
    assert len(done) == 2, f"expected 2 done, got {len(done)}"
    print("\nWORKER E2E PASSED")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
