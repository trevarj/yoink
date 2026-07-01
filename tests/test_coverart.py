from __future__ import annotations

from pathlib import Path

import httpx

from yoink.config import Config
from yoink.metadata import coverart as camod
from yoink.metadata.coverart import CoverArtArchive


def _client(tmp_path: Path) -> CoverArtArchive:
    cfg = Config(cache_dir=tmp_path / "cache")
    return CoverArtArchive(cfg)


def test_front_cover_downloads_and_caches(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"png-data",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(camod.httpx, "get", fake_get)

    client = _client(tmp_path)
    art = client.front_cover("rel1")
    assert art is not None
    assert art.data == b"png-data"
    assert art.mime == "image/png"

    cached = client.front_cover("rel1")
    assert cached == art
    assert len(calls) == 1


def test_front_cover_caches_missing_404(monkeypatch, tmp_path):
    calls = 0

    def fake_get(url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(camod.httpx, "get", fake_get)

    client = _client(tmp_path)
    assert client.front_cover("rel1") is None
    assert client.front_cover("rel1") is None
    assert calls == 1


def test_front_cover_skips_non_image(monkeypatch, tmp_path):
    def fake_get(url, **kwargs):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"nope",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(camod.httpx, "get", fake_get)

    assert _client(tmp_path).front_cover("rel1") is None
