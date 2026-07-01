"""Cover Art Archive client keyed by MusicBrainz release IDs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import Config

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class CoverArt:
    data: bytes
    mime: str


class CoverArtArchive:
    """Fetch and cache front cover art for a concrete MusicBrainz release."""

    def __init__(self, config: Config) -> None:
        self.root = config.art_cache_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.user_agent = config.user_agent

    def front_cover(self, release_mbid: str) -> CoverArt | None:
        base = self._base(release_mbid)
        image = base.with_suffix(".img")
        mime_path = base.with_suffix(".mime")
        missing = base.with_suffix(".missing")

        if image.exists():
            try:
                mime = mime_path.read_text().strip() if mime_path.exists() else "image/jpeg"
                return CoverArt(image.read_bytes(), mime or "image/jpeg")
            except OSError:
                return None
        if missing.exists():
            return None

        url = f"https://coverartarchive.org/release/{release_mbid}/front-500"
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
                timeout=20.0,
            )
        except httpx.HTTPError:
            return None

        if resp.status_code == 404:
            self._touch(missing)
            return None
        if resp.status_code >= 400:
            return None

        mime = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not mime.startswith("image/") or not resp.content:
            self._touch(missing)
            return None

        try:
            tmp = image.with_suffix(".tmp")
            tmp.write_bytes(resp.content)
            tmp.replace(image)
            mime_path.write_text(mime)
        except OSError:
            return None
        return CoverArt(resp.content, mime)

    def _base(self, release_mbid: str) -> Path:
        return self.root / f"{_SAFE.sub('_', release_mbid)}-front"

    @staticmethod
    def _touch(path: Path) -> None:
        try:
            path.touch()
        except OSError:
            pass
