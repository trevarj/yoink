"""The yoink Textual application.

Browse tab: search MusicBrainz release-groups (with a description column), and
highlight any result to preview its tracklist + durations on the right. Press
``a`` to queue the highlighted album. Queue tab: a live album/track status view
with remove (``x``) and requeue-failed (``R``) actions, refreshed from the
SQLite queue while the background worker downloads + tags.
"""

from __future__ import annotations

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Static,
    TabbedContent,
    TabPane,
)

from ..config import Config
from ..jobs import db as dbmod
from ..jobs.db import Database
from ..jobs.worker import Worker
from ..metadata.musicbrainz import MusicBrainz
from ..models import Release, ReleaseGroupHit
from ..youtube.search import YouTubeMusic
from .resolve import ResolveScreen


def _fmt_ms(ms: int | None) -> str:
    if not ms:
        return "—"
    s = round(ms / 1000)
    return f"{s // 60}:{s % 60:02d}"


def _status_text(status: str) -> Text:
    """Render machine statuses as compact, readable state labels."""
    palette = {
        dbmod.ALBUM_DONE: ("#68d391", "done"),
        dbmod.TRACK_DONE: ("#68d391", "done"),
        dbmod.TRACK_TAGGED: ("#68d391", "tagged"),
        dbmod.ALBUM_FAILED: ("#ff6b7a", "failed"),
        dbmod.TRACK_FAILED: ("#ff6b7a", "failed"),
        dbmod.TRACK_NEEDS_REVIEW: ("#f4c95d", "review"),
        dbmod.ALBUM_DOWNLOADING: ("#67d4e8", "downloading"),
        dbmod.TRACK_DOWNLOADING: ("#67d4e8", "downloading"),
        dbmod.ALBUM_RESOLVING: ("#a99cff", "resolving"),
        dbmod.TRACK_MATCHING: ("#a99cff", "matching"),
        dbmod.ALBUM_QUEUED: ("#8b98a8", "queued"),
        dbmod.TRACK_QUEUED: ("#8b98a8", "queued"),
    }
    color, label = palette.get(status, ("#8b98a8", status.replace("_", " ")))
    value = Text("● ", style=color)
    value.append(label)
    return value


class YoinkApp(App):
    TITLE = "yoink"
    SUB_TITLE = "build a library worth keeping"
    CSS_PATH = "yoink.tcss"

    # Free C-n/C-p for emacs-style table navigation; the command palette moves
    # to Ctrl+Shift+P (Textual defaults Ctrl+P to it).
    COMMAND_PALETTE_BINDING = "ctrl+shift+p"

    BINDINGS = [
        Binding("a", "enqueue", "Queue album", show=False),
        Binding("/", "focus_search", "Search", show=False),
        Binding("x", "remove_album", "Remove", show=False),
        Binding("R", "requeue_album", "Requeue failed", show=False),
        Binding("m", "resolve_track", "Resolve track", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("b", "goto_browse", "Browse tab", show=False),
        Binding("q", "quit", "Quit"),
        Binding("1", "goto_browse", show=False),
        Binding("2", "goto_queue", show=False),
        # Emacs-style table navigation. Hidden from the footer; Input already
        # binds C-a/e/b/f so these only apply when a DataTable has focus.
        Binding("ctrl+n", "emacs_down", show=False),
        Binding("ctrl+p", "emacs_up", show=False),
        Binding("ctrl+f", "emacs_right", show=False),
        Binding("ctrl+b", "emacs_left", show=False),
        Binding("ctrl+v", "emacs_page_down", show=False),
        Binding("alt+v", "emacs_page_up", show=False),
        Binding("ctrl+a", "emacs_home", show=False),
        Binding("ctrl+e", "emacs_end", show=False),
        Binding("alt+n", "next_tab", show=False),
        Binding("alt+p", "prev_tab", show=False),
        Binding("alt+q", "goto_queue", show=False),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.config.ensure_dirs()
        self.db = Database(config.db_path)
        self.db.reset_inflight()  # resume any work interrupted by a prior run
        self.mb = MusicBrainz(config)
        self.yt = YouTubeMusic()  # for manual track resolution
        self._results: list[ReleaseGroupHit] = []
        self._albums: list[dbmod.AlbumJob] = []
        self._tracks: list[dbmod.TrackJob] = []
        self._resolving_track_id: int | None = None
        self._preview_cache: dict[str, Release | None] = {}
        self._preview_inflight: set[str] = set()
        self._preview_mbid: str | None = None
        self.worker: Worker | None = None

    # --- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="browse"):
            with TabPane("⌕  Discover", id="browse"):
                with Vertical(id="browse_page", classes="page"):
                    with Container(id="search_panel"):
                        yield Static("DISCOVER MUSIC", classes="eyebrow")
                        yield Input(
                            placeholder="Artist, album, or both…",
                            id="search",
                        )
                        yield Static(
                            "Search MusicBrainz  ·  Enter to preview  ·  A to add to queue",
                            classes="quiet hint",
                        )
                    with Horizontal(id="browse_body"):
                        with Vertical(id="catalog_pane", classes="pane"):
                            with Horizontal(classes="section_heading"):
                                yield Static("Albums", classes="section_title")
                                yield Static("Ready to search", id="results_count", classes="quiet")
                            yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
                        with Vertical(id="preview_pane", classes="pane"):
                            yield Static("ALBUM PREVIEW", classes="eyebrow")
                            yield Static(
                                "Choose an album\n"
                                "[dim]Its tracklist and release details will appear here.[/dim]",
                                id="preview_header",
                            )
                            yield DataTable(id="preview", cursor_type="row")
            with TabPane("⇣  Queue", id="queue"):
                with Vertical(id="queue_page", classes="page"):
                    with Horizontal(id="queue_overview"):
                        yield Static(
                            "[b]0[/b]\n[dim]ALBUMS[/dim]", id="metric_albums", classes="metric"
                        )
                        yield Static(
                            "[b]0[/b]\n[dim]TRACKS DONE[/dim]", id="metric_done", classes="metric"
                        )
                        yield Static(
                            "[b]0[/b]\n[dim]NEED ATTENTION[/dim]",
                            id="metric_review",
                            classes="metric warning_metric",
                        )
                    with Horizontal(id="queue_body"):
                        with Vertical(id="albums_pane", classes="pane"):
                            with Horizontal(classes="section_heading"):
                                yield Static("Albums", classes="section_title")
                                yield Static("Newest first", classes="quiet")
                            yield DataTable(id="albums", cursor_type="row", zebra_stripes=True)
                        with Vertical(id="tracks_pane", classes="pane"):
                            yield Static("TRACKS", classes="eyebrow")
                            yield Static(
                                "Select an album to inspect its tracks.", id="queue_detail"
                            )
                            yield DataTable(id="tracks", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="activity_bar"):
            yield Static("●", id="activity_indicator")
            yield Static("Worker ready", id="activity")
            yield Static("↑/↓ move   Enter preview   A queue   / search", id="nav_hint")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.query_one("#results", DataTable).add_columns(
            "Artist", "Album", "Year", "Type", "Notes"
        )
        self.query_one("#preview", DataTable).add_columns("#", "Title", "Time")
        self.query_one("#albums", DataTable).add_columns("Artist", "Album", "Status", "Done")
        self.query_one("#tracks", DataTable).add_columns("#", "Title", "Status", "Score", "Note")
        if "set contact" in self.config.mb_contact:
            self.notify(
                "Set 'mb_contact' in config.toml (MusicBrainz etiquette).",
                severity="warning",
                timeout=8,
            )
        self.worker = Worker(self.config, self.db, progress_cb=self._on_progress)
        self.worker.start()
        self.set_interval(1.5, self.action_refresh)
        self.query_one("#search", Input).focus()

    def on_unmount(self) -> None:
        if self.worker:
            self.worker.stop()

    # --- search -----------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self.notify(f"Searching “{query}”…", timeout=2)
            self.query_one("#results_count", Static).update("Searching…")
            self._set_activity(f"Searching MusicBrainz for “{query}”")
            self._search(query)

    @work(thread=True, exclusive=True, group="search")
    def _search(self, query: str) -> None:
        try:
            hits = self.mb.search_albums(query)
        except Exception as e:  # network / MB error
            self.call_from_thread(self.notify, f"Search failed: {e}", severity="error")
            self.call_from_thread(self.query_one("#results_count", Static).update, "Search failed")
            return
        self.call_from_thread(self._show_results, hits)

    def _show_results(self, hits: list[ReleaseGroupHit]) -> None:
        self._results = hits
        table = self.query_one("#results", DataTable)
        table.clear()
        for h in hits:
            table.add_row(h.artist, h.title, str(h.year or ""), h.primary_type or "", h.description)
        self.query_one("#results_count", Static).update(
            f"{len(hits)} result{'s' if len(hits) != 1 else ''}"
        )
        self._set_activity("Search complete" if hits else "No albums matched that search")
        self._clear_preview()
        if hits:
            table.focus()

    # --- tracklist preview ------------------------------------------------
    def _clear_preview(self) -> None:
        self._preview_mbid = None
        self.query_one("#preview_header", Static).update(
            "Choose an album\n[dim]Its tracklist and release details will appear here.[/dim]"
        )
        self.query_one("#preview", DataTable).clear()

    def _preview(self, hit: ReleaseGroupHit) -> None:
        self._preview_mbid = hit.mbid
        if hit.mbid in self._preview_cache:
            self._render_preview(hit.mbid, self._preview_cache[hit.mbid])
            return
        self.query_one("#preview_header", Static).update(
            f"{hit.artist} — {hit.title}\nLoading tracks…"
        )
        self.query_one("#preview", DataTable).clear()
        if hit.mbid not in self._preview_inflight:
            self._preview_inflight.add(hit.mbid)
            self._fetch_preview(hit.mbid)

    @work(thread=True, group="preview")
    def _fetch_preview(self, rg_mbid: str) -> None:
        try:
            release = self.mb.release_for_group(rg_mbid)
        except Exception:
            release = None
        self.call_from_thread(self._store_preview, rg_mbid, release)

    def _store_preview(self, rg_mbid: str, release: Release | None) -> None:
        self._preview_cache[rg_mbid] = release
        self._preview_inflight.discard(rg_mbid)
        if self._preview_mbid == rg_mbid:  # still the highlighted row
            self._render_preview(rg_mbid, release)

    def _render_preview(self, rg_mbid: str, release: Release | None) -> None:
        header = self.query_one("#preview_header", Static)
        table = self.query_one("#preview", DataTable)
        table.clear()
        if release is None:
            header.update("No release found for that album.")
            return
        total = sum(t.duration_ms or 0 for t in release.tracks)
        meta = " · ".join(
            x for x in (str(release.year or ""), release.country, _fmt_ms(total)) if x
        )
        header.update(f"{release.artist} — {release.title}\n{release.track_count} tracks · {meta}")
        for t in release.tracks:
            label = (
                f"{t.disc}.{t.position:02d}"
                if any(tk.disc > 1 for tk in release.tracks)
                else f"{t.position:02d}"
            )
            table.add_row(label, t.title, _fmt_ms(t.duration_ms))

    # --- enqueue ----------------------------------------------------------
    def action_enqueue(self) -> None:
        table = self.query_one("#results", DataTable)
        if not self._results or table.cursor_row is None:
            return
        idx = table.cursor_row
        if not (0 <= idx < len(self._results)):
            return
        hit = self._results[idx]
        self.notify(f"Resolving {hit.artist} — {hit.title}…", timeout=3)
        self._enqueue(hit.mbid)

    @work(thread=True, group="enqueue")
    def _enqueue(self, rg_mbid: str) -> None:
        try:
            release = self.mb.release_for_group(rg_mbid)
        except Exception as e:
            self.call_from_thread(self.notify, f"Resolve failed: {e}", severity="error")
            return
        if release is None:
            self.call_from_thread(self.notify, "No release found for that album", severity="error")
            return
        album_id = self.db.enqueue_release(release)
        verb = "Queued" if album_id else "Already queued"
        self.call_from_thread(
            self.notify,
            f"{verb}: {release.artist} — {release.title} ({release.track_count} tracks)",
        )

    # --- queue view -------------------------------------------------------
    def action_refresh(self) -> None:
        albums = self.db.list_album_jobs()
        self._albums = albums
        table = self.query_one("#albums", DataTable)
        cursor = table.cursor_row
        table.clear()
        total_done = 0
        total_review = 0
        for a in albums:
            prog = self.db.album_progress(a.id)
            done = prog.get(dbmod.TRACK_DONE, 0)
            flagged = prog.get(dbmod.TRACK_NEEDS_REVIEW, 0) + prog.get(dbmod.TRACK_FAILED, 0)
            total_done += done
            total_review += flagged
            cell = f"{done}/{a.total_tracks}" + (f" ⚠{flagged}" if flagged else "")
            table.add_row(a.artist, a.album, _status_text(a.status), cell)
        self.query_one("#metric_albums", Static).update(
            f"[b]{len(albums)}[/b]\n[dim]ALBUM{'S' if len(albums) != 1 else ''}[/dim]"
        )
        self.query_one("#metric_done", Static).update(
            f"[b]{total_done}[/b]\n[dim]TRACKS DONE[/dim]"
        )
        self.query_one("#metric_review", Static).update(
            f"[b]{total_review}[/b]\n[dim]NEED ATTENTION[/dim]"
        )
        if cursor is not None and albums:
            table.move_cursor(row=min(cursor, len(albums) - 1))
        self._refresh_tracks()

    def _selected_album(self) -> dbmod.AlbumJob | None:
        table = self.query_one("#albums", DataTable)
        idx = table.cursor_row
        if idx is None or not (0 <= idx < len(self._albums)):
            return None
        return self._albums[idx]

    def _refresh_tracks(self) -> None:
        tracks_table = self.query_one("#tracks", DataTable)
        album = self._selected_album()
        cursor = tracks_table.cursor_row
        tracks_table.clear()
        if album is None:
            self._tracks = []
            self.query_one("#queue_detail", Static).update("Select an album to inspect its tracks.")
            return
        self.query_one("#queue_detail", Static).update(
            f"[b]{album.artist} — {album.album}[/b]\n"
            f"[dim]{album.year or 'Year unknown'} · {album.total_tracks} tracks[/dim]"
            + (f"\n[red]{escape(album.error)}[/red]" if album.error else "")
        )
        self._tracks = self.db.list_tracks(album.id)
        for t in self._tracks:
            score = (
                "manual"
                if t.manual_video_id
                else (f"{t.match_score:.0f}" if t.match_score is not None else "")
            )
            note = (t.error or "")[:48]
            tracks_table.add_row(
                f"{t.disc_no}.{t.track_no:02d}", t.title, _status_text(t.status), score, note
            )
        if cursor is not None and self._tracks:
            tracks_table.move_cursor(row=min(cursor, len(self._tracks) - 1))

    def _selected_track(self) -> dbmod.TrackJob | None:
        table = self.query_one("#tracks", DataTable)
        idx = table.cursor_row
        if idx is None or not (0 <= idx < len(self._tracks)):
            return None
        return self._tracks[idx]

    def action_resolve_track(self) -> None:
        track = self._selected_track()
        if track is None:
            return
        self._resolving_track_id = track.id
        self.push_screen(ResolveScreen(track, self.yt, self.config), self._on_resolved)

    def _on_resolved(self, video_id: str | None) -> None:
        if not video_id or self._resolving_track_id is None:
            return
        self.db.set_manual_source(self._resolving_track_id, video_id)
        self.notify(f"Manual source set ({video_id}); track requeued.")
        self.action_refresh()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Local DB read only -- safe to do on highlight.
        if event.data_table.id == "albums":
            self._refresh_tracks()
        elif event.data_table.id == "results":
            idx = event.cursor_row
            if idx is not None and 0 <= idx < len(self._results):
                hit = self._results[idx]
                if self._preview_mbid != hit.mbid:
                    self.query_one("#preview_header", Static).update(
                        f"[b]{hit.artist} — {hit.title}[/b]\n"
                        f"[dim]{hit.year or 'Year unknown'} · {hit.primary_type or 'Release'}"
                        " · Press Enter to load tracks[/dim]"
                    )
                    self.query_one("#preview", DataTable).clear()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a search result previews its tracklist (one API call), so we
        # don't hammer MusicBrainz while scrolling.
        if event.data_table.id == "results":
            idx = event.cursor_row
            if idx is not None and 0 <= idx < len(self._results):
                self._preview(self._results[idx])

    # --- queue actions ----------------------------------------------------
    def action_remove_album(self) -> None:
        album = self._selected_album()
        if album is None:
            return
        if self.worker:
            self.worker.cancel_album(album.id)
        self.db.delete_album(album.id)
        self.notify(f"Removed: {album.artist} — {album.album}")
        self.action_refresh()

    def action_requeue_album(self) -> None:
        album = self._selected_album()
        if album is None:
            return
        n = self.db.requeue_album(album.id)
        self.notify(f"Requeued {n} track(s) in {album.album}" if n else "Nothing to requeue")
        self.action_refresh()

    # --- worker progress --------------------------------------------------
    def _on_progress(self, track_id: int, frac: float | None, status: str) -> None:
        pct = f" {frac * 100:.0f}%" if frac is not None else ""
        self.call_from_thread(self._set_activity, f"track {track_id}: {status}{pct}")

    def _set_activity(self, text: str) -> None:
        self.query_one("#activity", Static).update(text)

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    # --- tab navigation ---------------------------------------------------
    _TABS = ("browse", "queue")

    def action_goto_browse(self) -> None:
        self.query_one(TabbedContent).active = "browse"
        self.query_one("#search", Input).focus()

    def action_goto_queue(self) -> None:
        self.query_one(TabbedContent).active = "queue"
        self.action_refresh()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "queue":
            self.query_one("#nav_hint", Static).update(
                "↑/↓ move   M resolve   R retry   X remove   1 discover"
            )
            self.action_refresh()
        else:
            self.query_one("#nav_hint", Static).update(
                "↑/↓ move   Enter preview   A queue   / search   2 queue"
            )

    def _switch_tab(self, delta: int) -> None:
        tc = self.query_one(TabbedContent)
        cur = tc.active
        try:
            i = self._TABS.index(cur)
        except ValueError:
            i = 0
        tc.active = self._TABS[(i + delta) % len(self._TABS)]
        if tc.active == "browse":
            self.query_one("#search", Input).focus()
        else:
            self.action_refresh()

    def action_next_tab(self) -> None:
        self._switch_tab(+1)

    def action_prev_tab(self) -> None:
        self._switch_tab(-1)

    # --- emacs table navigation ------------------------------------------
    # Forward to the focused DataTable's own (synchronous) cursor actions so the
    # keys work only over tables; Input keeps its built-in emacs bindings.
    def _dt(self, fn: str) -> None:
        fw = self.focused
        if isinstance(fw, DataTable):
            getattr(fw, fn)()

    def action_emacs_down(self) -> None:
        self._dt("action_cursor_down")

    def action_emacs_up(self) -> None:
        self._dt("action_cursor_up")

    def action_emacs_right(self) -> None:
        self._dt("action_cursor_right")

    def action_emacs_left(self) -> None:
        self._dt("action_cursor_left")

    def action_emacs_page_down(self) -> None:
        self._dt("action_page_down")

    def action_emacs_page_up(self) -> None:
        self._dt("action_page_up")

    def action_emacs_home(self) -> None:
        fw = self.focused
        if isinstance(fw, DataTable) and fw.row_count:
            fw.move_cursor(row=0, animate=False)

    def action_emacs_end(self) -> None:
        fw = self.focused
        if isinstance(fw, DataTable) and fw.row_count:
            fw.move_cursor(row=fw.row_count - 1, animate=False)
