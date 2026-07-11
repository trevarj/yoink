"""Configuration and XDG-compliant paths for yoink.

Config lives at ``$XDG_CONFIG_HOME/yoink/config.toml``. Durable state (the job
queue DB, yt-dlp download archive, isolated beets library) lives under
``$XDG_STATE_HOME/yoink``; cached HTTP responses and cover art under
``$XDG_CACHE_HOME/yoink``.  Nothing here holds secrets -- MusicBrainz needs no
auth, only a descriptive User-Agent contact string.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from platformdirs import PlatformDirs
from yt_dlp.cookies import SUPPORTED_BROWSERS, SUPPORTED_KEYRINGS

from . import __version__

_dirs = PlatformDirs(appname="yoink", appauthor=False)

_CODEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_COOKIES_FROM_BROWSER_RE = re.compile(
    r"""(?x)
    (?P<name>[^+:]+)
    (?:\s*\+\s*(?P<keyring>[^:]+))?
    (?:\s*:\s*(?!:)(?P<profile>.+?))?
    (?:\s*::\s*(?P<container>.+))?
    """
)


def _music_default() -> Path:
    """Honour $XDG_MUSIC_DIR (user-dirs.dirs), else ~/Music."""
    env = os.environ.get("XDG_MUSIC_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Music"


@dataclass(frozen=True)
class Config:
    # Where the finished, tagged library tree is written.
    music_dir: Path = field(default_factory=_music_default)
    # Durable state + caches.
    state_dir: Path = field(default_factory=lambda: Path(_dirs.user_state_dir))
    cache_dir: Path = field(default_factory=lambda: Path(_dirs.user_cache_dir))
    # Contact string baked into the MusicBrainz User-Agent (required by MB).
    mb_contact: str = "yoink (set contact in config.toml)"
    # Matching: hard duration gate (seconds) and minimum accept score.
    duration_gate_s: float = 3.0
    duration_soft_s: float = 7.0
    min_match_score: float = 6.0
    # Parallel track downloads within an album. Keep modest to stay friendly to
    # YouTube and avoid throttling.
    download_concurrency: int = 3
    # Preferred audio codec for extraction.
    audio_codec: str = "opus"
    # Optional yt-dlp browser-cookie source.  This is the same syntax as
    # --cookies-from-browser, e.g. "brave:/path/to/Brave-Browser/Default".
    # The config stores only a browser/profile reference, never cookie data.
    cookies_from_browser: str | None = None
    # Minimum audio bitrate (kbps) to accept a candidate; 0 disables the probe.
    # Default on so low-bitrate reuploads are flagged for review rather than
    # silently saved. The probe adds one extract_info round-trip per track.
    min_audio_bitrate: float = 128.0
    # Strip featured-guest artists ("feat.", "ft.", "featuring") from the track's
    # artist tag so a featured single doesn't split from its album in a player.
    strip_featured_artists: bool = True
    # Write ReplayGain (R128) track + album gain tags so an album's tracks play
    # at a consistent volume. Non-destructive: tags only, no audio re-encode.
    # Album gain is computed across all tracks at album-completion time.
    replaygain: bool = True
    # Tagging backend: "beets" (canonical library import) or "mutagen" (direct,
    # deterministic write of the known MusicBrainz metadata).
    tagger: str = "beets"

    def __post_init__(self) -> None:
        """Reject invalid values before they reach worker threads or tools."""
        for name in ("music_dir", "state_dir", "cache_dir"):
            if not isinstance(getattr(self, name), Path):
                raise ValueError(f"{name} must be a filesystem path")

        if not isinstance(self.mb_contact, str) or not self.mb_contact.strip():
            raise ValueError("mb_contact must be a non-empty string")

        for name in ("duration_gate_s", "duration_soft_s", "min_match_score"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.duration_soft_s < self.duration_gate_s:
            raise ValueError("duration_soft_s must be greater than or equal to duration_gate_s")

        if isinstance(self.download_concurrency, bool) or not isinstance(
            self.download_concurrency, int
        ):
            raise ValueError("download_concurrency must be an integer")
        if self.download_concurrency < 1:
            raise ValueError("download_concurrency must be at least 1")

        if not isinstance(self.audio_codec, str) or not _CODEC_RE.fullmatch(
            self.audio_codec
        ):
            raise ValueError("audio_codec must be a non-empty codec name")

        if self.cookies_from_browser is not None:
            if not isinstance(
                self.cookies_from_browser, str
            ) or not self.cookies_from_browser.strip():
                raise ValueError("cookies_from_browser must be a non-empty string or null")
            if not _COOKIES_FROM_BROWSER_RE.fullmatch(self.cookies_from_browser):
                raise ValueError(
                    "cookies_from_browser must use yt-dlp's "
                    "BROWSER[+KEYRING][:PROFILE][::CONTAINER] syntax"
                )
            browser, _, keyring, _ = _cookies_from_browser_parts(
                self.cookies_from_browser
            )
            if browser not in SUPPORTED_BROWSERS:
                raise ValueError(
                    f"unsupported browser for cookies_from_browser: {browser}"
                )
            if keyring is not None and keyring not in SUPPORTED_KEYRINGS:
                raise ValueError(
                    f"unsupported keyring for cookies_from_browser: {keyring.lower()}"
                )

        if isinstance(self.min_audio_bitrate, bool) or not isinstance(
            self.min_audio_bitrate, (int, float)
        ):
            raise ValueError("min_audio_bitrate must be a number")
        if not math.isfinite(self.min_audio_bitrate) or self.min_audio_bitrate < 0:
            raise ValueError("min_audio_bitrate must be a finite non-negative number")

        for name in ("strip_featured_artists", "replaygain"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be true or false")
        if not isinstance(self.tagger, str) or self.tagger not in {"beets", "mutagen"}:
            raise ValueError("tagger must be 'beets' or 'mutagen'")

    # --- Derived paths -----------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.state_dir / "queue.db"

    @property
    def archive_path(self) -> Path:
        return self.state_dir / "archive.txt"

    @property
    def staging_dir(self) -> Path:
        return self.state_dir / "staging"

    @property
    def beets_dir(self) -> Path:
        return self.state_dir / "beets"

    @property
    def mb_cache_dir(self) -> Path:
        return self.cache_dir / "musicbrainz"

    @property
    def art_cache_dir(self) -> Path:
        return self.cache_dir / "coverart"

    @property
    def user_agent(self) -> str:
        return f"yoink/{__version__} ( {self.mb_contact} )"

    @property
    def cookies_from_browser_options(
        self,
    ) -> tuple[str, str | None, str | None, str | None] | None:
        """Return ``cookiesfrombrowser`` in yt-dlp's Python API shape."""
        if self.cookies_from_browser is None:
            return None
        return _cookies_from_browser_parts(self.cookies_from_browser)

    def ensure_dirs(self) -> None:
        for p in (
            self.state_dir,
            self.cache_dir,
            self.staging_dir,
            self.beets_dir,
            self.mb_cache_dir,
            self.art_cache_dir,
            self.music_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


def config_path() -> Path:
    return Path(_dirs.user_config_dir) / "config.toml"


def _cookies_from_browser_parts(
    value: str,
) -> tuple[str, str | None, str | None, str | None]:
    """Parse a validated yt-dlp ``--cookies-from-browser`` value."""
    match = _COOKIES_FROM_BROWSER_RE.fullmatch(value)
    assert match is not None
    browser, keyring, profile, container = match.group(
        "name", "keyring", "profile", "container"
    )
    return browser.lower(), profile, keyring.upper() if keyring else None, container


def load_config() -> Config:
    """Load config.toml, overlaying any present keys onto the defaults."""
    cfg = Config()
    path = config_path()
    if not path.exists():
        return cfg
    data = tomllib.loads(path.read_text())
    known = {
        "music_dir",
        "state_dir",
        "cache_dir",
        "mb_contact",
        "duration_gate_s",
        "duration_soft_s",
        "min_match_score",
        "download_concurrency",
        "audio_codec",
        "cookies_from_browser",
        "min_audio_bitrate",
        "strip_featured_artists",
        "replaygain",
        "tagger",
    }
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"unknown configuration key(s): {', '.join(unknown)}")
    overrides: dict = {}
    for key in (
        "mb_contact",
        "duration_gate_s",
        "duration_soft_s",
        "min_match_score",
        "download_concurrency",
        "audio_codec",
        "cookies_from_browser",
        "min_audio_bitrate",
        "strip_featured_artists",
        "replaygain",
        "tagger",
    ):
        if key in data:
            overrides[key] = data[key]
    for key in ("music_dir", "state_dir", "cache_dir"):
        if key in data:
            if not isinstance(data[key], str) or not data[key].strip():
                raise ValueError(f"{key} must be a non-empty path string")
            overrides[key] = Path(data[key]).expanduser()
    return replace(cfg, **overrides)
