"""Modal screen to manually resolve a track that couldn't be auto-matched.

Shows the YouTube Music candidates (scored, with duration deltas) for a stuck
track and lets the user either pick one or paste a YouTube URL / videoId. The
chosen videoId is returned via ``dismiss`` so the worker can download it
verbatim.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
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
    ]

    CSS = """
    ResolveScreen { align: center middle; }
    #dialog {
        width: 90%; height: 80%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    #resolve_header { height: auto; text-style: bold; margin-bottom: 1; }
    #candidates { height: 1fr; }
    #url { dock: bottom; margin-top: 1; }
    #resolve_help { dock: bottom; height: 1; color: $text-muted; }
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
            br = (
                f"  ·  {self.track.audio_bitrate:.0f}k"
                if self.track.audio_bitrate
                else ""
            )
            yield Static(
                f"Resolve: {self.track.artist} — {self.track.title}  "
                f"[{_fmt(self.track.duration_ms)}]{reason}{br}",
                id="resolve_header",
            )
            yield DataTable(id="candidates", cursor_type="row", zebra_stripes=True)
            yield Input(
                placeholder="…or paste a YouTube URL / videoId, then Enter",
                id="url",
            )
            yield Static(
                "Enter: pick highlighted candidate · Esc: cancel", id="resolve_help"
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

    def _populate(self, scored: list[tuple[Candidate, float]]) -> None:
        table = self.query_one("#candidates", DataTable)
        table.clear()
        self._cands = [c for c, _ in scored]
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
