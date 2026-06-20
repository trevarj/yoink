"""Headless TUI boot test: mounts the app, populates results, renders the queue.

No network: MusicBrainz is monkeypatched and the worker only polls an empty DB.
Run: PYTHONPATH=src .venv/bin/python tests/smoke_tui.py
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

from textual.widgets import DataTable, Static

from yoink.config import load_config
from yoink.models import Release, ReleaseGroupHit, Track
from yoink.tui.app import YoinkApp

HITS = [
    ReleaseGroupHit(
        mbid="rg1", title="Discovery", artist="Daft Punk", artist_mbid="ad1",
        primary_type="Album", year=2001,
    ),
    ReleaseGroupHit(
        mbid="rg2", title="Alive 2007", artist="Daft Punk", artist_mbid="ad1",
        primary_type="Album", year=2007, secondary_types=("Live",),
        disambiguation="live album",
    ),
]
RELEASE = Release(
    mbid="rel1", title="Discovery", artist="Daft Punk", artist_mbid="ad1",
    date="2001-03-12", year=2001, country="XW", track_count=2,
    tracks=(
        Track(1, 1, "One More Time", "Daft Punk", 320000, "rec1"),
        Track(2, 1, "Aerodynamic", "Daft Punk", 213000, "rec2"),
    ),
)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="yoink-tui-"))
    cfg = replace(
        load_config(), state_dir=tmp / "state", cache_dir=tmp / "cache",
        music_dir=tmp / "music", tagger="mutagen",
        mb_contact="smoke (x@y.z)",
    )
    app = YoinkApp(cfg)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Widgets present and columns initialized.
        results = app.query_one("#results", DataTable)
        assert len(results.columns) == 5, len(results.columns)
        assert app.query_one("#activity", Static) is not None

        # Render search results (the sync UI path).
        app._show_results(HITS)
        await pilot.pause()
        assert results.row_count == 2, results.row_count
        # Description column populated for the live album.
        assert "Live" in str(results.get_row_at(1)[4]), results.get_row_at(1)

        # Preview pane: seed the cache to avoid network, then press Enter on the
        # focused first result to trigger the select handler.
        app._preview_cache[HITS[0].mbid] = RELEASE
        results.focus()
        results.move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause()
        preview = app.query_one("#preview", DataTable)
        assert preview.row_count == 2, preview.row_count
        assert "One More Time" in str(preview.get_row_at(0)[1])

        # Enqueue directly (bypass the thread worker) then render the queue.
        cfg.ensure_dirs()
        app.db.enqueue_release(RELEASE)
        app.action_refresh()
        await pilot.pause()
        albums = app.query_one("#albums", DataTable)
        tracks = app.query_one("#tracks", DataTable)
        assert albums.row_count == 1, albums.row_count
        assert tracks.row_count == 2, tracks.row_count
        print("albums row:", albums.get_row_at(0))
        print("tracks rows:", [tracks.get_row_at(i) for i in range(tracks.row_count)])

    print("TUI BOOT TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
