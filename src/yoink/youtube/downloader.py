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

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import yt_dlp

from ..config import Config

# progress_cb(fraction: float | None, status: str)
ProgressCb = Callable[[float | None, str], None]

_NON_AUDIO_SUFFIXES = {"webp", "jpg", "jpeg", "png", "part", "ytdl", "tmp"}
_CLI_PROBE_PREFIX = "__YOINK_PROBE__\t"
_CLI_PROGRESS_PREFIX = "__YOINK_PROGRESS__\t"


class DownloadError(Exception):
    pass


@dataclass(frozen=True)
class AudioQuality:
    """Audio stream selected by the downloader, without downloading it.

    ``bitrate_kbps`` is None when it could not be measured -- callers must treat
    that as "unknown" and proceed, never reject, so a good stream we can't read
    the bitrate of isn't blocked.
    """

    video_id: str
    bitrate_kbps: float | None
    ext: str | None
    acodec: str | None
    filesize: int | None


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
        self.config = config
        self.staging = config.staging_dir
        self.codec = config.audio_codec
        self.staging.mkdir(parents=True, exist_ok=True)

    def _opts(
        self, progress_cb: ProgressCb | None, output_stem: str = "%(id)s"
    ) -> dict:
        opts: dict = {
            "format": "bestaudio/best",
            "format_sort": [f"acodec:{self.codec}", "abr"],
            "outtmpl": {"default": str(self.staging / f"{output_stem}.%(ext)s")},
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
        if (cookies := self.config.cookies_from_browser_options) is not None:
            opts["cookiesfrombrowser"] = cookies
        return opts

    def probe_audio(self, video_id: str) -> AudioQuality | None:
        """Inspect the best audio stream without downloading.

        Returns None on any failure (network / geo / parse) so callers never
        crash on a probe; treat None as "unknown quality" (proceed, don't
        reject).
        """
        if self.config.yt_dlp_command:
            return self._probe_audio_cli(video_id)

        opts = self._opts(None)
        opts["skip_download"] = True
        opts["quiet"] = True
        url = f"https://music.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError:
            return None
        if not info:
            return None
        # YouTube routinely reports abr=None for opus; tbr is the trustworthy
        # field for audio-only streams. Fall back to abr when tbr is absent.
        def _br(f: dict) -> float:
            return float(f.get("tbr") or f.get("abr") or 0)

        # ``extract_info`` processes the configured format selector even with
        # download=False and overlays the selected format onto the top-level
        # info dict. Inspect that selection instead of independently picking
        # the highest bitrate, which may be a different codec than download().
        if info.get("format_id") and info.get("acodec") != "none":
            best = info
        else:
            # Unit-test/simple-extractor fallback: mirror our codec-first sort,
            # then choose the highest bitrate within that codec.
            fmts = [f for f in info.get("formats", []) if f.get("vcodec") == "none"]
            if not fmts:
                fmts = [info]
            preferred = [f for f in fmts if f.get("acodec") == self.codec]
            best = max(preferred or fmts, key=_br)
        br = _br(best)
        return AudioQuality(
            video_id=video_id,
            bitrate_kbps=br or None,
            ext=best.get("ext"),
            acodec=best.get("acodec"),
            filesize=best.get("filesize") or best.get("filesize_approx"),
        )

    def download(self, video_id: str, progress_cb: ProgressCb | None = None) -> Path:
        """Download + extract one track. Returns the staged audio file path."""
        if self.config.yt_dlp_command:
            return self._download_cli(video_id, progress_cb)

        url = f"https://music.youtube.com/watch?v={video_id}"
        # Multiple album tracks can intentionally resolve to one video. Give
        # every invocation its own files (including thumbnail/temporary files)
        # so concurrent yt-dlp postprocessing cannot overwrite a sibling.
        output_stem = f"{video_id}-{uuid4().hex}"
        try:
            with yt_dlp.YoutubeDL(self._opts(progress_cb, output_stem)) as ydl:
                rc = ydl.download([url])
        except yt_dlp.utils.DownloadError as e:  # network / unavailable / geo
            raise DownloadError(str(e)) from e
        if rc != 0:
            raise DownloadError(f"yt-dlp returned {rc} for {video_id}")
        return self._locate(output_stem)

    def _cli_common_args(self) -> list[str]:
        """Arguments Yoink owns even when yt-dlp is supplied by a wrapper."""
        args = [
            "--no-playlist",
            "--format",
            "bestaudio/best",
            "--format-sort",
            f"acodec:{self.codec},abr",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--extractor-retries",
            "2",
        ]
        if self.config.cookies_from_browser:
            args.extend(["--cookies-from-browser", self.config.cookies_from_browser])
        return args

    def _cli_command(self, *args: str) -> list[str]:
        assert self.config.yt_dlp_command is not None
        # A command name/path is one argv element.  Do not split or execute it
        # through a shell: user configuration must not become shell syntax.
        return [self.config.yt_dlp_command, *args]

    def _probe_audio_cli(self, video_id: str) -> AudioQuality | None:
        command = self._cli_command(
            *self._cli_common_args(),
            "--skip-download",
            "--no-warnings",
            "--print",
            _CLI_PROBE_PREFIX
            + "%(format_id)s\t%(acodec)s\t%(ext)s\t%(tbr)s\t%(abr)s"
            + "\t%(filesize)s\t%(filesize_approx)s",
            f"https://music.youtube.com/watch?v={video_id}",
        )
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False)
        except OSError:
            return None
        if result.returncode != 0:
            return None
        for line in reversed(result.stdout.splitlines()):
            if line.startswith(_CLI_PROBE_PREFIX):
                return self._parse_cli_probe(video_id, line.removeprefix(_CLI_PROBE_PREFIX))
        return None

    @staticmethod
    def _parse_cli_probe(video_id: str, payload: str) -> AudioQuality | None:
        fields = payload.split("\t")
        if len(fields) != 7:
            return None
        _, acodec, ext, tbr, abr, filesize, filesize_approx = fields

        def number(value: str) -> float | None:
            try:
                return float(value) if value not in {"", "NA"} else None
            except ValueError:
                return None

        def size(value: str) -> int | None:
            try:
                return int(value) if value not in {"", "NA"} else None
            except ValueError:
                return None

        return AudioQuality(
            video_id=video_id,
            bitrate_kbps=number(tbr) or number(abr),
            ext=ext or None,
            acodec=acodec or None,
            filesize=size(filesize) or size(filesize_approx),
        )

    def _download_cli(self, video_id: str, progress_cb: ProgressCb | None) -> Path:
        output_stem = f"{video_id}-{uuid4().hex}"
        command = self._cli_command(
            *self._cli_common_args(),
            "--no-warnings",
            "--newline",
            "--progress-template",
            "download:"
            + _CLI_PROGRESS_PREFIX
            + "%(progress.downloaded_bytes)s\t%(progress.total_bytes)s"
            + "\t%(progress.total_bytes_estimate)s",
            "--output",
            str(self.staging / f"{output_stem}.%(ext)s"),
            "--write-thumbnail",
            "--sponsorblock-remove",
            "music_offtopic",
            "--extract-audio",
            "--audio-format",
            self.codec,
            "--embed-metadata",
            "--embed-thumbnail",
            f"https://music.youtube.com/watch?v={video_id}",
        )
        output: list[str] = []
        try:
            with subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            ) as proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    output.append(line.rstrip())
                    self._emit_cli_progress(line, progress_cb)
                rc = proc.wait()
        except OSError as e:
            raise DownloadError(f"could not run {self.config.yt_dlp_command}: {e}") from e
        if rc != 0:
            detail = "\n".join(line for line in output[-20:] if line)
            raise DownloadError(detail or f"yt-dlp returned {rc} for {video_id}")
        if progress_cb:
            progress_cb(1.0, "processing")
        return self._locate(output_stem)

    @staticmethod
    def _emit_cli_progress(line: str, progress_cb: ProgressCb | None) -> None:
        if not progress_cb or not line.startswith(_CLI_PROGRESS_PREFIX):
            return
        values = line.removeprefix(_CLI_PROGRESS_PREFIX).strip().split("\t")
        if len(values) != 3:
            return
        try:
            got = float(values[0])
            total = next(float(value) for value in values[1:] if value not in {"", "NA"})
        except (StopIteration, ValueError):
            progress_cb(None, "downloading")
            return
        progress_cb(got / total if total else None, "downloading")

    def _locate(self, output_stem: str) -> Path:
        preferred = self.staging / f"{output_stem}.{self.codec}"
        if preferred.exists():
            return preferred
        # Codec may differ if the preferred stream wasn't available.
        for m in sorted(self.staging.glob(f"{output_stem}.*")):
            if m.suffix.lstrip(".").lower() not in _NON_AUDIO_SUFFIXES:
                return m
        raise DownloadError(f"no output audio file produced for {output_stem}")
