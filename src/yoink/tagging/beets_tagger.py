"""Beets backend: non-interactive, isolated canonical import.

Runs ``beet import -q --search-id <mb_release_id>`` against a per-album staging
directory. Isolation via ``BEETSDIR`` keeps a private config + library DB so we
never touch the user's personal beets library. ``--search-id`` pins the exact
MusicBrainz release (no candidate search); ``-q`` + ``quiet_fallback: skip``
guarantee zero prompts in the worker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import Config

_CONFIG_TEMPLATE = """\
directory: {music_dir}
library: {library_db}

import:
  move: yes
  write: yes
  quiet: yes
  quiet_fallback: skip
  resume: no
  incremental: no
  log: {import_log}

ui:
  color: no

paths:
  default: $albumartist/$album/$track $title
  singleton: $albumartist/Non-Album/$title
  comp: Various Artists/$album/$track $title

# 'musicbrainz' is the metadata source plugin -- it must stay enabled or
# --search-id has no resolver (beets defaults to it, but we override `plugins`).
plugins: musicbrainz fromfilename
musicbrainz:
  searchlimit: 5

# We pin the exact release with --search-id, so trust it: relax the strong-match
# threshold and disable distance penalties that would otherwise make quiet mode
# skip a correct-but-imperfect match (e.g. slight length/source differences).
match:
  strong_rec_thresh: 0.90
  max_rec:
    missing_tracks: strong
    unmatched_tracks: strong
"""


class BeetsError(Exception):
    pass


def _beet_executable() -> str:
    found = shutil.which("beet")
    if found:
        return found
    # Same venv as the running interpreter.
    sibling = Path(sys.executable).parent / "beet"
    if sibling.exists():
        return str(sibling)
    raise BeetsError("'beet' executable not found on PATH or in venv")


class BeetsTagger:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.beetsdir = config.beets_dir
        self.beetsdir.mkdir(parents=True, exist_ok=True)
        self._write_config()

    def _write_config(self) -> None:
        text = _CONFIG_TEMPLATE.format(
            music_dir=self.config.music_dir,
            library_db=self.beetsdir / "library.db",
            import_log=self.beetsdir / "import.log",
        )
        (self.beetsdir / "config.yaml").write_text(text)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["BEETSDIR"] = str(self.beetsdir)
        return env

    def import_album(self, album_dir: Path, mb_release_id: str) -> str:
        """Import one album directory; returns beets' combined output.

        Raises BeetsError on a non-zero exit. A 'skip' (no confident match) is
        not an exception -- the caller inspects the returned log.
        """
        cmd = [
            _beet_executable(),
            "import",
            "-q",
            "--search-id",
            mb_release_id,
            str(album_dir),
        ]
        proc = subprocess.run(
            cmd,
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=900,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise BeetsError(f"beet import failed ({proc.returncode}):\n{out}")
        return out
