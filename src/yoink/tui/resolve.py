"""Modal screen to manually resolve a track that couldn't be auto-matched.

Shows the YouTube Music candidates (scored, with duration deltas) for a stuck
track and lets the user either pick one or paste a YouTube URL / videoId. The
chosen videoId is returned via ``dismiss`` so the worker can download it
verbatim.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from ..config import Config
from ..jobs.db import TrackJob
from ..models import Track
from ..youtube.matcher import score_candidates
from ..youtube.search import Candidate, YouTubeMusic, parse_video_id


def _fmt(ms: int | None) -> str:
    if not ms:
        return "—"
    s = round(ms / 1000)
    return f"{s // 60}:{s % 60:02d}"


class ResolveScreen(ModalScreen[str | None]):
    """Returns the chosen videoId, or None if cancelled."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "choose", "Choose"),
        ("/", "focus_url", "Paste URL"),
    ]

    CSS = """
    ResolveScreen { align: center middle; background: #05080db3; }
    #dialog {
        width: 92%; max-width: 124; height: 84%;
        border: tall #67d4e8; background: #111823; padding: 1 2;
    }
    #resolve_kicker { height: 1; color: #67d4e8; text-style: bold; }
    #resolve_header {
        height: auto; min-height: 3; text-style: bold; padding: 1;
        margin-bottom: 1; background: #172130; border-left: thick #a99cff;
    }
    #candidate_heading { height: 2; }
    #candidate_title { width: 1fr; text-style: bold; }
    #candidate_count { width: auto; color: #8290a3; text-align: right; }
    #candidates { height: 1fr; }
    #source_panel {
        height: 6; margin-top: 1; padding: 0 1;
        background: #0a0e14; border: solid #263448;
    }
    #source_label { height: 2; padding-top: 1; color: #8290a3; }
    #url { height: 3; border: tall #263448; background: #172130; }
    #url:focus { border: tall #67d4e8; }
    #resolve_help { height: 1; color: #8290a3; text-align: center; }
    """

    def __init__(self, track: TrackJob, yt: YouTubeMusic, config: Config) -> None:
        super().__init__()
        self.track = track
        self.yt = yt
        self.config = config
        self._cands: list[Candidate] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            reason = f"  ·  reason: {self.track.error}" if self.track.error else ""
            br = f"  ·  {self.track.audio_bitrate:.0f}k" if self.track.audio_bitrate else ""
            yield Static("MANUAL SOURCE", id="resolve_kicker")
            yield Static(
                f"{self.track.artist} — {self.track.title}\n"
                f"[dim]{_fmt(self.track.duration_ms)}{reason}{br}[/dim]",
                id="resolve_header",
            )
            with Horizontal(id="candidate_heading"):
                yield Static("Best matches", id="candidate_title")
                yield Static("Searching…", id="candidate_count")
            yield DataTable(id="candidates", cursor_type="row", zebra_stripes=True)
            with Container(id="source_panel"):
                yield Static("Have the exact source?", id="source_label")
                yield Input(
                    placeholder="Paste a YouTube URL or video ID…",
                    id="url",
                )
            yield Static(
                "↑/↓ choose  ·  Enter confirm  ·  / paste URL  ·  Esc cancel",
                id="resolve_help",
            )

    def on_mount(self) -> None:
        table = self.query_one("#candidates", DataTable)
        table.add_columns("Score", "Type", "Time", "Δ", "Title")
        table.add_row("", "", "", "", "Searching YouTube Music…")
        self._load()

    @work(thread=True, exclusive=True)
    def _load(self) -> None:
        try:
            cands = self.yt.search_track(self.track.artist, self.track.title, limit=10)
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._show_error, str(e))
            return
        track = Track(
            position=self.track.track_no,
            disc=self.track.disc_no,
            title=self.track.title,
            artist=self.track.artist,
            duration_ms=self.track.duration_ms,
        )
        scored = score_candidates(
            track,
            cands,
            gate_s=self.config.duration_gate_s,
            soft_s=self.config.duration_soft_s,
        )
        self.app.call_from_thread(self._populate, scored)

    def _show_error(self, msg: str) -> None:
        table = self.query_one("#candidates", DataTable)
        table.clear()
        table.add_row("", "", "", "", f"Search failed: {msg}")
        self.query_one("#candidate_count", Static).update("Search failed")

    def _populate(self, scored: list[tuple[Candidate, float]]) -> None:
        table = self.query_one("#candidates", DataTable)
        table.clear()
        self._cands = [c for c, _ in scored]
        self.query_one("#candidate_count", Static).update(
            f"{len(self._cands)} candidate{'s' if len(self._cands) != 1 else ''}"
        )
        if not self._cands:
            table.add_row("", "", "", "", "No candidates — paste a URL below.")
            self.query_one("#url", Input).focus()
            return
        for cand, score in scored:
            if self.track.duration_ms and cand.duration_s:
                delta = f"{cand.duration_s - round(self.track.duration_ms / 1000):+d}s"
            else:
                delta = "?"
            secs = cand.duration_s
            time = f"{secs // 60}:{secs % 60:02d}" if secs else "—"
            table.add_row(f"{score:.0f}", cand.result_type, time, delta, cand.title)
        table.focus()

    def action_choose(self) -> None:
        table = self.query_one("#candidates", DataTable)
        idx = table.cursor_row
        if self._cands and idx is not None and 0 <= idx < len(self._cands):
            self.dismiss(self._cands[idx].video_id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if self._cands and idx is not None and 0 <= idx < len(self._cands):
            self.dismiss(self._cands[idx].video_id)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        vid = parse_video_id(event.value)
        if vid:
            self.dismiss(vid)
        else:
            self.notify("Couldn't parse a videoId from that.", severity="error")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_focus_url(self) -> None:
        self.query_one("#url", Input).focus()
