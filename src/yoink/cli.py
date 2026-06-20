"""Command-line entry point for yoink."""

from __future__ import annotations

import argparse

from . import __version__
from .config import config_path, load_config

_SAMPLE_CONFIG = """\
# yoink configuration

# Required by MusicBrainz etiquette: a contact (email or URL) for the
# User-Agent. Set this to something real.
mb_contact = "you@example.com"

# Tagging backend: "beets" (canonical library import) or "mutagen" (direct).
tagger = "beets"

# Audio codec yt-dlp extracts to.
audio_codec = "opus"

# Matching tolerances.
duration_gate_s = 3.0
duration_soft_s = 7.0
min_match_score = 6.0

# Number of tracks downloaded in parallel within an album.
download_concurrency = 3

# Library output directory (defaults to $XDG_MUSIC_DIR or ~/Music).
# music_dir = "/home/you/Music"
"""


def _write_config() -> None:
    path = config_path()
    if path.exists():
        print(f"config already exists: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SAMPLE_CONFIG)
    print(f"wrote sample config: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="yoink",
        description="TUI music browser that yoinks full albums off YouTube Music.",
    )
    parser.add_argument("--version", action="version", version=f"yoink {__version__}")
    parser.add_argument(
        "--write-config",
        action="store_true",
        help="write a sample config.toml and exit",
    )
    args = parser.parse_args()

    if args.write_config:
        _write_config()
        return

    config = load_config()
    config.ensure_dirs()
    # Import lazily so --version / --write-config don't pull in Textual.
    from .tui.app import YoinkApp

    YoinkApp(config).run()


if __name__ == "__main__":
    main()
