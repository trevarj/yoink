"""yt-dlp wrapper: fetch one track's bestaudio into the staging dir.

Single-video downloads (by videoId) keep one uniform path for both the
album-as-playlist and per-track strategies, and let the matcher verify every
track. Dedupe is owned by the SQLite queue, so yt-dlp's download-archive is
deliberately omitted -- it would silently skip re-downloads after beets moves a
file out of staging.

SponsorBlock's ``music_offtopic`` category trims non-music intros/outros that
"Topic" uploads sometimes carry. Audio is extracted to the configured codec
(opus by default) with embedded cover art and metadata; beets later moves and
renames into the final library tree.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yt_dlp

from ..config import Config

# progress_cb(fraction: float | None, status: str)
ProgressCb = Callable[[float | None, str], None]

_NON_AUDIO_SUFFIXES = {"webp", "jpg", "jpeg", "png", "part", "ytdl", "tmp"}


class DownloadError(Exception):
    pass


def _make_hook(cb: ProgressCb):
    def hook(d: dict) -> None:
        status = d.get("status", "")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            got = d.get("downloaded_bytes")
            frac = (got / total) if (total and got is not None) else None
            cb(frac, "downloading")
        elif status == "finished":
            cb(1.0, "processing")  # download done; postprocessing follows

    return hook


class Downloader:
    def __init__(self, config: Config) -> None:
        self.staging = config.staging_dir
        self.codec = config.audio_codec
        self.staging.mkdir(parents=True, exist_ok=True)

    def _opts(self, progress_cb: ProgressCb | None) -> dict:
        opts: dict = {
            "format": "bestaudio/best",
            "format_sort": [f"acodec:{self.codec}", "abr"],
            "outtmpl": {"default": str(self.staging / "%(id)s.%(ext)s")},
            "writethumbnail": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "ignoreerrors": False,
            # Never hang the queue on a stalled connection: bound socket reads
            # and retry a few times before failing the track.
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 2,
            # Postprocessor order matters: fetch SponsorBlock segments and strip
            # them before extracting audio, then write tags + embed art.
            "postprocessors": [
                {"key": "SponsorBlock", "categories": ["music_offtopic"]},
                {
                    "key": "ModifyChapters",
                    "remove_sponsor_segments": ["music_offtopic"],
                },
                {"key": "FFmpegExtractAudio", "preferredcodec": self.codec},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
        }
        if progress_cb:
            opts["progress_hooks"] = [_make_hook(progress_cb)]
        return opts

    def download(self, video_id: str, progress_cb: ProgressCb | None = None) -> Path:
        """Download + extract one track. Returns the staged audio file path."""
        url = f"https://music.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(self._opts(progress_cb)) as ydl:
                rc = ydl.download([url])
        except yt_dlp.utils.DownloadError as e:  # network / unavailable / geo
            raise DownloadError(str(e)) from e
        if rc != 0:
            raise DownloadError(f"yt-dlp returned {rc} for {video_id}")
        return self._locate(video_id)

    def _locate(self, video_id: str) -> Path:
        preferred = self.staging / f"{video_id}.{self.codec}"
        if preferred.exists():
            return preferred
        # Codec may differ if the preferred stream wasn't available.
        for m in sorted(self.staging.glob(f"{video_id}.*")):
            if m.suffix.lstrip(".").lower() not in _NON_AUDIO_SUFFIXES:
                return m
        raise DownloadError(f"no output audio file produced for {video_id}")
